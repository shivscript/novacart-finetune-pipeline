"""S3-triggered Lambda. When prep_summary.json lands, kick off a Bedrock fine-tuning job and record it in DynamoDB."""

import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = os.environ.get('BUCKET_NAME', 'novacart-finetune')
BEDROCK_ROLE_ARN = os.environ['BEDROCK_ROLE_ARN']
BASE_MODEL_ID = os.environ.get('BASE_MODEL_ID', 'amazon.nova-micro-v1:0:128k')
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'novacart-finetune-jobs')

EPOCHS = os.environ.get('EPOCHS', '3')
BATCH_SIZE = os.environ.get('BATCH_SIZE', '1')
LEARNING_RATE = os.environ.get('LEARNING_RATE', '0.00001')
LEARNING_RATE_WARMUP = os.environ.get('LEARNING_RATE_WARMUP', '0')


def _default_model_prefix(model_id: str) -> str:
    short = model_id.split('.', 1)[-1].split('-v')[0].split(':')[0]
    return short or 'custom'


MODEL_NAME_PREFIX = os.environ.get('MODEL_NAME_PREFIX', _default_model_prefix(BASE_MODEL_ID))

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')
bedrock_client = boto3.client('bedrock')
jobs_table = boto3.resource('dynamodb').Table(DYNAMODB_TABLE)


class ValidationError(Exception):
    """Permanent failure — return, don't raise, so Lambda doesn't retry."""


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = unquote_plus(event['Records'][0]['s3']['object']['key'])
        logger.info(f"Processing trigger file: s3://{bucket}/{key}")

        if not key.endswith('prep_summary.json'):
            logger.warning(f"Skipping non-target file: {key}")
            return {'statusCode': 200, 'body': 'Skipped'}

        prep_metadata = read_prep_metadata(bucket, key)
        logger.info(f"Prep metadata: {json.dumps(prep_metadata)}")

        validate_metadata(prep_metadata)

        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        job_name = f"novacart-{MODEL_NAME_PREFIX}-finetune-{timestamp}"
        custom_model_name = f"novacart-{MODEL_NAME_PREFIX}-{timestamp}"

        job_response = start_finetune_job(
            job_name=job_name,
            custom_model_name=custom_model_name,
            training_s3_uri=prep_metadata['training_s3_uri'],
            validation_s3_uri=prep_metadata['validation_s3_uri'],
            output_s3_uri=f's3://{bucket}/output/',
        )

        job_arn = job_response['jobArn']
        logger.info(f"Bedrock job started: {job_arn}")

        track_job_in_dynamodb(
            job_id=job_name,
            job_arn=job_arn,
            custom_model_name=custom_model_name,
            prep_metadata=prep_metadata,
            hyperparameters={
                'epochs': EPOCHS,
                'batch_size': BATCH_SIZE,
                'learning_rate': LEARNING_RATE,
            },
        )

        result = {
            'job_id': job_name,
            'job_arn': job_arn,
            'custom_model_name': custom_model_name,
            'base_model': BASE_MODEL_ID,
            'training_examples': prep_metadata.get('training_count'),
            'validation_examples': prep_metadata.get('validation_count'),
            'status': 'InProgress',
            'started_at': timestamp,
        }

        logger.info(f"SUCCESS: {json.dumps(result)}")
        return {'statusCode': 200, 'body': json.dumps(result)}

    except ValidationError as e:
        logger.error(f"VALIDATION_FAILED (no retry): {e}")
        return {'statusCode': 400, 'body': json.dumps({'error': 'ValidationError', 'message': str(e)})}

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        permanent_errors = {'AccessDeniedException', 'ValidationException', 'ResourceNotFoundException'}
        if error_code in permanent_errors:
            logger.error(f"BEDROCK_PERMANENT_ERROR (no retry): {error_code} - {e}")
            return {'statusCode': 400, 'body': json.dumps({'error': error_code, 'message': str(e)})}

        logger.error(f"BEDROCK_TRANSIENT_ERROR (will retry): {error_code} - {e}", exc_info=True)
        raise

    except Exception as e:
        logger.error(f"TRANSIENT_FAILURE (will retry): {e}", exc_info=True)
        raise


def read_prep_metadata(bucket: str, key: str) -> dict:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(response['Body'].read().decode('utf-8'))


def validate_metadata(metadata: dict) -> None:
    required = ['training_s3_uri', 'validation_s3_uri', 'training_count', 'validation_count']
    missing = [f for f in required if f not in metadata]
    if missing:
        raise ValidationError(f"Metadata missing required fields: {missing}")

    if metadata['training_count'] < 32:
        raise ValidationError(
            f"Bedrock fine-tuning requires at least 32 training examples. "
            f"Got only {metadata['training_count']}."
        )


def start_finetune_job(
    job_name: str,
    custom_model_name: str,
    training_s3_uri: str,
    validation_s3_uri: str,
    output_s3_uri: str,
) -> dict:
    hyperparameters = {
        'epochCount': EPOCHS,
        'batchSize': BATCH_SIZE,
        'learningRate': LEARNING_RATE,
        'learningRateWarmupSteps': LEARNING_RATE_WARMUP,
    }

    logger.info(
        f"Starting Bedrock job: name={job_name}, base_model={BASE_MODEL_ID}, "
        f"hyperparameters={hyperparameters}"
    )

    return bedrock_client.create_model_customization_job(
        jobName=job_name,
        customModelName=custom_model_name,
        roleArn=BEDROCK_ROLE_ARN,
        baseModelIdentifier=BASE_MODEL_ID,
        trainingDataConfig={'s3Uri': training_s3_uri},
        validationDataConfig={'validators': [{'s3Uri': validation_s3_uri}]},
        outputDataConfig={'s3Uri': output_s3_uri},
        hyperParameters=hyperparameters,
        customizationType='FINE_TUNING',
        jobTags=[
            {'key': 'Project', 'value': 'NovaCart'},
            {'key': 'Pipeline', 'value': 'AutomatedFineTuning'},
            {'key': 'Environment', 'value': 'Sandbox'},
        ],
    )


def track_job_in_dynamodb(
    job_id: str,
    job_arn: str,
    custom_model_name: str,
    prep_metadata: dict,
    hyperparameters: dict,
) -> None:
    item = {
        'job_id': job_id,
        'job_arn': job_arn,
        'custom_model_name': custom_model_name,
        'base_model': BASE_MODEL_ID,
        'status': 'InProgress',
        'started_at': datetime.now(timezone.utc).isoformat(),
        'training_s3_uri': prep_metadata.get('training_s3_uri', ''),
        'validation_s3_uri': prep_metadata.get('validation_s3_uri', ''),
        'training_count': prep_metadata.get('training_count', 0),
        'validation_count': prep_metadata.get('validation_count', 0),
        'source_file': prep_metadata.get('source_file', ''),
        'hyperparameters': hyperparameters,
    }
    try:
        jobs_table.put_item(Item=item)
        logger.info(f"DynamoDB record created for job_id={job_id}")
    except ClientError as e:
        logger.error(f"Failed to write to DynamoDB: {e}")
