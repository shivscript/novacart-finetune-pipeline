"""S3-triggered Lambda. Validates a raw CSV, dedups, splits 80/20, and writes Bedrock Conversation JSONL for Nova fine-tuning."""

import csv
import io
import json
import logging
import os
import random
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = os.environ.get('BUCKET_NAME', 'novacart-finetune')
TRAIN_SPLIT = float(os.environ.get('TRAIN_SPLIT', '0.8'))
MIN_PROMPT_LENGTH = int(os.environ.get('MIN_PROMPT_LENGTH', '10'))
MIN_COMPLETION_LENGTH = int(os.environ.get('MIN_COMPLETION_LENGTH', '20'))
MIN_TOTAL_EXAMPLES = int(os.environ.get('MIN_TOTAL_EXAMPLES', '50'))
RANDOM_SEED = int(os.environ.get('RANDOM_SEED', '42'))
SYSTEM_PROMPT = os.environ.get(
    'SYSTEM_PROMPT',
    "You are NovaCart's friendly customer support assistant. "
    "Respond in a warm, professional tone. Use numbered steps when helpful. "
    "Always close with '— Team NovaCart 💙'."
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')


class ValidationError(Exception):
    """Permanent failure — return, don't raise, so Lambda doesn't retry."""


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = unquote_plus(event['Records'][0]['s3']['object']['key'])
        logger.info(f"Processing file: s3://{bucket}/{key}")

        if not key.startswith('raw/') or not key.endswith('.csv'):
            logger.warning(f"Skipping non-target file: {key}")
            return {'statusCode': 200, 'body': 'Skipped'}

        raw_examples = download_and_parse_csv(bucket, key)
        logger.info(f"Loaded {len(raw_examples)} raw examples")

        clean_examples = validate_examples(raw_examples)
        logger.info(f"Validated: {len(clean_examples)} clean examples")

        if len(clean_examples) < MIN_TOTAL_EXAMPLES:
            raise ValidationError(
                f"Only {len(clean_examples)} valid examples. "
                f"Need at least {MIN_TOTAL_EXAMPLES} for meaningful fine-tuning."
            )

        random.seed(RANDOM_SEED)
        random.shuffle(clean_examples)
        split_idx = int(len(clean_examples) * TRAIN_SPLIT)
        train_data = clean_examples[:split_idx]
        val_data = clean_examples[split_idx:]
        logger.info(f"Split: {len(train_data)} train / {len(val_data)} validation")

        train_jsonl = convert_to_nova_jsonl(train_data)
        val_jsonl = convert_to_nova_jsonl(val_data)

        upload_to_s3(bucket, 'training/training.jsonl', train_jsonl)
        upload_to_s3(bucket, 'validation/validation.jsonl', val_jsonl)

        metadata = {
            'source_file': key,
            'total_raw': len(raw_examples),
            'total_valid': len(clean_examples),
            'training_count': len(train_data),
            'validation_count': len(val_data),
            'train_split': TRAIN_SPLIT,
            'format': 'bedrock-conversation-2024',
            'target_model_family': 'nova',
            'training_s3_uri': f's3://{bucket}/training/training.jsonl',
            'validation_s3_uri': f's3://{bucket}/validation/validation.jsonl',
        }
        upload_to_s3(bucket, 'metadata/prep_summary.json', json.dumps(metadata, indent=2))

        logger.info(f"SUCCESS: {json.dumps(metadata)}")
        return {'statusCode': 200, 'body': json.dumps(metadata)}

    except ValidationError as e:
        logger.error(f"VALIDATION_FAILED (no retry): {e}")
        return {'statusCode': 400, 'body': json.dumps({'error': 'ValidationError', 'message': str(e)})}

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        logger.error(f"AWS_CLIENT_ERROR (will retry): {error_code}", exc_info=True)
        raise

    except Exception as e:
        logger.error(f"TRANSIENT_FAILURE (will retry): {e}", exc_info=True)
        raise


def download_and_parse_csv(bucket: str, key: str) -> list:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    csv_content = response['Body'].read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(csv_content))
    return list(reader)


def validate_examples(raw_examples: list) -> list:
    clean = []
    seen_prompts = set()
    skipped = {'empty': 0, 'too_short': 0, 'duplicate': 0, 'missing_field': 0}

    for row in raw_examples:
        prompt = row.get('customer_query', '').strip()
        completion = row.get('support_response', '').strip()

        if 'customer_query' not in row or 'support_response' not in row:
            skipped['missing_field'] += 1
            continue
        if not prompt or not completion:
            skipped['empty'] += 1
            continue
        if len(prompt) < MIN_PROMPT_LENGTH or len(completion) < MIN_COMPLETION_LENGTH:
            skipped['too_short'] += 1
            continue
        if prompt.lower() in seen_prompts:
            skipped['duplicate'] += 1
            continue

        seen_prompts.add(prompt.lower())
        clean.append({'prompt': prompt, 'completion': completion})

    logger.info(f"Validation skips: {json.dumps(skipped)}")
    return clean


def convert_to_nova_jsonl(examples: list) -> str:
    lines = []
    for ex in examples:
        record = {
            "schemaVersion": "bedrock-conversation-2024",
            "system": [{"text": SYSTEM_PROMPT}],
            "messages": [
                {"role": "user", "content": [{"text": ex['prompt']}]},
                {"role": "assistant", "content": [{"text": ex['completion']}]},
            ],
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    return '\n'.join(lines)


def upload_to_s3(bucket: str, key: str, content: str) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode('utf-8'),
        ContentType='application/jsonl' if key.endswith('.jsonl') else 'application/json',
    )
    logger.info(f"Uploaded: s3://{bucket}/{key} ({len(content)} bytes)")
