import functools
import json
import shutil
import subprocess
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.conditions import Attr, Key

from common.logger_utility import *

s3Resource = boto3.resource('s3')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')


def __run_in_parallel(job_generator, max_workers):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(job) for job in job_generator]
    for future in futures:
        exception = future.exception()
        if exception is not None:
            raise exception


def get_size(filename):
    st = os.stat(filename)
    return st.st_size


def update_batch_status(batch_id, status, is_historical):
    LoggerUtility.log_info(
        "Place holder to push the batch id and status to ES - {} - {} - {} ".format(batch_id, status, is_historical))


def __initiate_manifest_process(batch_id, event):
    yield from (
        functools.partial(__process_manifest_files, batch_id, event['table_type'], event['is_historical'] == 'true'),
    )


def delete_dir(file_download_path):
    try:
        shutil.rmtree(file_download_path)
    except OSError as e:
        LoggerUtility.log_error("Error: %s - %s." % (e.filename, e.strerror))
        raise e


def __process_manifest_files_values_returned(response, table_name, curated_records_dict, manifest_dict, batch_id,
                                             is_historical, file_download_path):
    records = response['Items']
    while response.get('LastEvaluatedKey'):
        LoggerUtility.log_info("More records present, so querying the index to get additional records "
                               "for table - {}".format(table_name))
        response = curated_records_dict["table"].query(
            index_name=curated_records_dict["index_name"],
            KeyConditionExpression=Key('BatchId').eq(batch_id) & Key('DataTableName').eq(table_name),
            ExclusiveStartKey=response['LastEvaluatedKey'],
            FilterExpression=Attr('IsHistorical').eq(str(is_historical))
        )
        records.extend(response['Items'])
    LoggerUtility.log_info("Completed fetching all records from index for table - {} "
                           "with count - {}".format(table_name, len(records)))
    entries_list = []
    total_curated_records_count = 0
    records_per_state_dict = dict()
    count = 0
    for record in records:
        entries = dict()
        entries['url'] = record['S3Key']
        count += 1
        # Download S3 file to /tmp/<UUID>/ folder
        s3Resource.Bucket(curated_records_dict["bucket_name"]).download_file(
            record['S3Key'].split(curated_records_dict["bucket_name"] + "/", 1)[1],
            file_download_path + str(count) + ".gz"
        )
        entries['mandatory'] = bool("true")
        entries_list.append(entries)
        total_curated_records_count += int(record["TotalNumCuratedRecords"])
        state = record["State"]
        if state not in records_per_state_dict:
            records_per_state_dict[state] = int(record["TotalNumCuratedRecords"])
        else:
            current_records = records_per_state_dict.get(state)
            total_num_records = current_records + int(record["TotalNumCuratedRecords"])
            records_per_state_dict[state] = total_num_records

    if len(entries_list) > 0:
        json_data = json.dumps(entries_list)
        modified_json = json.dumps('{"entries":' + json_data + '}')
        manifest_json = json.JSONDecoder().decode(modified_json)
        output_to_file = json.loads(manifest_json)
        manifest_file_name = "/tmp/" + str(uuid.uuid4()) + ".manifest"
        with open(manifest_file_name, 'w') as manifest_file:
            json.dump(output_to_file, manifest_file)

        manifest_s3_key = "manifest/" + batch_id + "/" + table_name + "/" + os.path.basename(manifest_file_name)
        s3 = boto3.client('s3')

        s3.upload_file(manifest_file_name, curated_records_dict["bucket_name"], manifest_s3_key)
        LoggerUtility.log_info(
            "Successfully uploaded manifest file to s3 for batch id - {} and table name - {}".format(batch_id,
                                                                                                     table_name)
        )
        # run subprocess commands to gunzip files present in /tmp/<UUID>/ folder
        # and then add all files to single gz file
        subprocess.call('gunzip ' + file_download_path + "*", shell=True)

        combined_file_name = str(uuid.uuid4()) + ".gz"
        combined_file_path = file_download_path + combined_file_name

        LoggerUtility.log_info('Combined file path - {}'.format(combined_file_path))

        subprocess.call('gzip -c ' + file_download_path + "/* > " + combined_file_path, shell=True)

        combined_s3_key = "manifest/" + batch_id + "/" + table_name + "/" + combined_file_name
        # File size of combined curated file
        combined_file_size = get_size(combined_file_path)
        s3.upload_file(combined_file_path, curated_records_dict["bucket_name"], combined_s3_key)

        # Get item if already exists
        response = manifest_dict["table"].query(
            index_name=manifest_dict["index_name"],
            KeyConditionExpression=Key('BatchId').eq(batch_id) & Key('TableName').eq(table_name),
            FilterExpression=Attr('IsHistorical').eq(is_historical)
        )
        manifest_id = str(uuid.uuid4())
        if response['Items']:
            LoggerUtility.log_info("Manifest Id already exists")
            manifest_id = response['Items'][0]['ManifestId']

        response = manifest_dict["table"].put_item(
            Item={
                "ManifestId": manifest_id,
                "BatchId": batch_id,
                "TableName": table_name,
                "ManifestS3Key": manifest_s3_key,
                "CombinedS3Key": combined_s3_key,
                "CombinedFileSize": combined_file_size,
                "IsHistorical": is_historical,
                "FileStatus": "open",
                "TotalCuratedRecordsCount": total_curated_records_count,
                "TotalCuratedRecordsByState": records_per_state_dict
            }
        )

        LoggerUtility.log_info("Response from put item - {}".format(response))
        LoggerUtility.log_info("Successfully created an item in dyanmodb table - {} for batch id - {} "
                               "and table name - {}".format(manifest_dict["table_name"], batch_id, table_name))


def __process_manifest_files(batch_id, table_name, is_historical):
    manifest_file_name = ""
    file_download_path = None
    try:
        LoggerUtility.log_info(
            "Batch id - {} and table_name - {} - is_historical - {}".format(batch_id, table_name, is_historical))
        update_batch_status(batch_id, 'PROCESSING', is_historical)

        manifest_table_name = os.environ['DDB_MANIFEST_TABLE_ARN'].split('/')[1]
        curated_records_table_name = os.environ['DDB_CURATED_RECORDS_TABLE_ARN'].split('/')[1]

        curated_records_dict = {
            "table_name": curated_records_table_name,
            "index_name": os.environ['DDB_CURATED_RECORDS_INDEX_NAME'],
            "table": dynamodb.Table(curated_records_table_name),
            "bucket_name": os.environ['CURATED_BUCKET_NAME']
        }

        manifest_dict = {
            "table_name": manifest_table_name,
            "index_name": os.environ['DDB_MANIFEST_INDEX_NAME'],
            "table": dynamodb.Table(manifest_table_name)
        }

        response = curated_records_dict["table"].query(
            index_name=curated_records_dict["index_name"],
            KeyConditionExpression=Key('BatchId').eq(batch_id) & Key('DataTableName').eq(table_name),
            FilterExpression=Attr('IsHistorical').eq(str(is_historical))
        )
        file_download_path = "/tmp/" + str(uuid.uuid4()) + "/"
        os.makedirs(file_download_path)

        """
        curated_records_table, curated_records_index_name, curated_bucket_name,
        manifest_files_table_name, manifest_index_name
        """

        if response['Count'] > 0:  # if values are returned
            __process_manifest_files_values_returned(response, table_name, curated_records_dict, manifest_dict,
                                                     batch_id, is_historical, file_download_path)
        else:
            LoggerUtility.log_info("No records to process for table - {}. Exiting the process".format(table_name))

    except Exception as e:
        LoggerUtility.log_error("Failed to upload manifest file for batch id - {} "
                                "and table name - {} with exception - {}".format(batch_id, table_name, e))
        traceback.print_exc()
        raise e
    finally:
        delete_dir(file_download_path)
        if manifest_file_name != "":
            os.unlink(manifest_file_name)


def generate_manifest_files(event):
    LoggerUtility.set_level()
    LoggerUtility.log_info("Initiating manifest process")
    is_historical = event['is_historical'] == 'true'
    data = {}
    batch_id = ""
    if 'batch_id' in event:
        batch_id = event['batch_id']
        LoggerUtility.log_info("Received batch id - {}".format(batch_id))

    try:
        if batch_id != "":
            __run_in_parallel(__initiate_manifest_process(batch_id, event), max_workers=15)
            update_batch_status(batch_id, 'COMPLETED', is_historical)

        LoggerUtility.log_info("Completed manifest process")
        data['batch_id'] = batch_id
        data['queueUrl'] = event['queueUrl']
        data['receiptHandle'] = event['receiptHandle']
        data['is_historical'] = event['is_historical']
        return data
    except Exception as e:
        LoggerUtility.log_error("Error occurred while processing batch - {} ".format(batch_id))
        update_batch_status(batch_id, 'ERROR', is_historical)
        raise e
