"""EventBridge-triggered Lambda. Fires when a Bedrock fine-tuning job reaches a terminal state — updates DynamoDB and emails via SNS.

Note: Bedrock's EventBridge payload uses `jobStatus` in the detail object, not `status`. Took a couple of failed runs to figure that out.
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = os.environ.get('BUCKET_NAME', 'novacart-finetune')
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'novacart-finetune-jobs')
SNS_TOPIC_ARN = os.environ['SNS_TOPIC_ARN']

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')
bedrock_client = boto3.client('bedrock')
sns_client = boto3.client('sns')
jobs_table = boto3.resource('dynamodb').Table(DYNAMODB_TABLE)


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        detail = event.get('detail', {})

        # Bedrock uses `jobStatus`; fall back to `status` for manual test events.
        new_status = detail.get('jobStatus') or detail.get('status', 'Unknown')

        job_arn = detail.get('jobArn', '')
        if not job_arn and event.get('resources'):
            job_arn = event['resources'][0]

        if not job_arn:
            logger.warning("Event missing jobArn; skipping")
            return {'statusCode': 200, 'body': 'Skipped (missing jobArn)'}

        logger.info(f"Job ARN: {job_arn}, status: {new_status}")

        if new_status not in {'Completed', 'Failed', 'Stopped'}:
            logger.info(f"Non-terminal status '{new_status}'; skipping")
            return {'statusCode': 200, 'body': f"Skipped (status: {new_status})"}

        job_details = get_bedrock_job_details(job_arn)
        job_name = job_details.get('jobName') or detail.get('jobName', 'unknown')

        training_metrics = fetch_training_metrics(job_details) if new_status == 'Completed' else {}

        update_dynamodb_status(job_name, new_status, job_details, training_metrics)
        send_sns_notification(job_name, new_status, job_details, training_metrics)

        result = {
            'job_id': job_name,
            'status': new_status,
            'metrics_recorded': bool(training_metrics),
            'notification_sent': True,
        }
        logger.info(f"SUCCESS: {json.dumps(result)}")
        return {'statusCode': 200, 'body': json.dumps(result)}

    except Exception as e:
        logger.error(f"FAILURE: {e}", exc_info=True)
        raise


def get_bedrock_job_details(job_arn: str) -> dict:
    response = bedrock_client.get_model_customization_job(jobIdentifier=job_arn)
    loggable = {k: v for k, v in response.items() if k != 'ResponseMetadata'}
    logger.info(f"Bedrock job details: {json.dumps(loggable, default=str)}")
    return response


def fetch_training_metrics(job_details: dict) -> dict:
    metrics = {}
    output_uri = job_details.get('outputDataConfig', {}).get('s3Uri', '')
    if not output_uri:
        logger.warning("No output S3 URI in job details")
        return metrics

    prefix = output_uri.replace(f's3://{BUCKET_NAME}/', '')
    try:
        response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix, MaxKeys=50)
    except ClientError as e:
        logger.warning(f"Failed to list metrics objects: {e}")
        return metrics

    for obj in response.get('Contents', []):
        key = obj['Key']
        if key.endswith('training_artifacts/step_wise_training_metrics.csv'):
            metrics['training_metrics_s3'] = f's3://{BUCKET_NAME}/{key}'

    if 'training_metrics_s3' in metrics:
        try:
            key = metrics['training_metrics_s3'].replace(f's3://{BUCKET_NAME}/', '')
            content = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)['Body'].read().decode('utf-8').strip()
            lines = content.split('\n')
            if len(lines) > 1:
                metrics['header'] = lines[0]
                metrics['last_training_row'] = lines[-1]
        except ClientError as e:
            logger.warning(f"Could not read training metrics file: {e}")

    return metrics


def update_dynamodb_status(job_id: str, new_status: str, job_details: dict, training_metrics: dict):
    jobs_table.update_item(
        Key={'job_id': job_id},
        UpdateExpression=(
            'SET #status = :status, finished_at = :finished_at, '
            'custom_model_arn = :custom_model_arn, '
            'training_metrics = :training_metrics, '
            'failure_message = :failure_message'
        ),
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={
            ':status': new_status,
            ':finished_at': datetime.now(timezone.utc).isoformat(),
            ':custom_model_arn': job_details.get('outputModelArn', 'N/A'),
            ':training_metrics': training_metrics or {},
            ':failure_message': job_details.get('failureMessage', '') or 'N/A',
        },
    )
    logger.info(f"DynamoDB updated for job_id={job_id}, status={new_status}")


def send_sns_notification(job_id: str, new_status: str, job_details: dict, training_metrics: dict):
    if new_status == 'Completed':
        subject = f"NovaCart fine-tuning complete: {job_id}"
        message = build_success_message(job_id, job_details, training_metrics)
    elif new_status == 'Failed':
        subject = f"NovaCart fine-tuning failed: {job_id}"
        message = build_failure_message(job_id, job_details)
    else:
        subject = f"NovaCart fine-tuning stopped: {job_id}"
        message = build_failure_message(job_id, job_details)

    sns_client.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
    logger.info(f"SNS notification sent: {subject}")


def build_success_message(job_id, job_details, metrics):
    lines = [
        "NovaCart fine-tuning job completed.",
        "",
        f"Job ID:        {job_id}",
        f"Base model:    {job_details.get('baseModelArn', 'N/A')}",
        f"Custom model:  {job_details.get('outputModelName', 'N/A')}",
        f"Model ARN:     {job_details.get('outputModelArn', 'N/A')}",
        f"Started:       {job_details.get('creationTime', 'N/A')}",
        f"Finished:      {job_details.get('endTime', 'N/A')}",
        "",
        "Hyperparameters:",
    ]
    for k, v in job_details.get('hyperParameters', {}).items():
        lines.append(f"  {k}: {v}")

    if metrics.get('last_training_row'):
        lines += [
            "",
            "Final training metrics:",
            f"  Header:   {metrics.get('header', '')}",
            f"  Last row: {metrics['last_training_row']}",
        ]
        if metrics.get('training_metrics_s3'):
            lines.append(f"  Full metrics: {metrics['training_metrics_s3']}")

    return '\n'.join(lines)


def build_failure_message(job_id, job_details):
    return '\n'.join([
        "NovaCart fine-tuning job did not complete successfully.",
        "",
        f"Job ID:    {job_id}",
        f"Status:    {job_details.get('status', 'Unknown')}",
        f"Failure:   {job_details.get('failureMessage', 'No details available')}",
        f"Started:   {job_details.get('creationTime', 'N/A')}",
        f"Ended:     {job_details.get('endTime', 'N/A')}",
    ])
