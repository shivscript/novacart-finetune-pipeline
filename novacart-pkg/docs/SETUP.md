# Setup & Deployment Guide

This guide walks through deploying the pipeline from scratch in your own AWS account. Region used throughout: **us-east-1**.

## Prerequisites

- An AWS account with permissions to create S3, Lambda, IAM, DynamoDB, SNS, and EventBridge resources
- Amazon Bedrock **model access** granted for Amazon Nova Micro (Bedrock console → Model access → request access)
- Python 3.12 for the Lambda runtime

---

## Step 1 — Create the S3 bucket

Create a bucket (e.g. `novacart-finetune`) and the following key prefixes (folders):

```
raw/         uploaded CSVs land here
training/    generated training JSONL
validation/  generated validation JSONL
metadata/    prep summaries (these trigger fine-tuning)
output/      Bedrock writes model artifacts here
```

---

## Step 2 — Create the Bedrock service role

Bedrock needs a role it can assume to read your training data and write model output.

1. IAM → Roles → Create role → Custom trust policy
2. Use the trust policy from [`iam-policies/bedrock-service-role.json`](../iam-policies/bedrock-service-role.json) (`TrustPolicy` block)
3. Attach the permissions from the same file (`PermissionsPolicy` block)
4. Name it `BedrockFineTuningRole` and note its ARN

---

## Step 3 — Create the DynamoDB table

| Setting       | Value                      |
| ------------- | -------------------------- |
| Table name    | `novacart-finetune-jobs` |
| Partition key | `job_id` (String)        |
| Capacity      | On-demand                  |

---

## Step 4 — Create the SNS topic

1. SNS → Create topic → **Standard** (not FIFO)
2. Name: `novacart-finetune-notifications`
3. Create an **email subscription** and confirm it via the email you receive
4. Note the topic ARN

---

## Step 5 — Deploy the Lambdas

Four Lambdas total: three drive the training pipeline (1–3), and one serves the fine-tuned model behind the chat UI (4). Deploy 1–3 now; come back for Lambda 4 (Step 8) after you've trained your first model.

For each Lambda: runtime **Python 3.12**, architecture **x86_64**.

### Lambda 1 — `novacart-data-prep`

- Code: [`src/lambdas/1_data_prep.py`](../src/lambdas/1_data_prep.py)
- Timeout: 2 minutes · Memory: 512 MB
- Execution role: attach `AWSLambdaBasicExecutionRole` (managed) + inline policy from [`iam-policies/data-prep-lambda-policy.json`](../iam-policies/data-prep-lambda-policy.json)
- Environment variables: `BUCKET_NAME=novacart-finetune`
- **Trigger:** S3 → bucket `novacart-finetune` → event `PUT` → prefix `raw/` → suffix `.csv`

### Lambda 2 — `novacart-finetune-trigger`

- Code: [`src/lambdas/2_finetune_trigger.py`](../src/lambdas/2_finetune_trigger.py)
- Timeout: 1 minute · Memory: 512 MB
- Execution role: basic execution + inline policy from [`iam-policies/finetune-trigger-policy.json`](../iam-policies/finetune-trigger-policy.json)
- Environment variables:
  ```
  BUCKET_NAME=novacart-finetune
  DYNAMODB_TABLE=novacart-finetune-jobs
  BEDROCK_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/BedrockFineTuningRole
  BASE_MODEL_ID=amazon.nova-micro-v1:0:128k
  EPOCHS=3
  BATCH_SIZE=1
  LEARNING_RATE=0.00001
  ```
- **Trigger:** S3 → same bucket → event `PUT` → prefix `metadata/` → suffix `.json`

### Lambda 3 — `novacart-finetune-completion`

- Code: [`src/lambdas/3_completion_handler.py`](../src/lambdas/3_completion_handler.py)
- Timeout: 1 minute · Memory: 512 MB
- Execution role: basic execution + inline policy from [`iam-policies/completion-handler-policy.json`](../iam-policies/completion-handler-policy.json)
- Environment variables:
  ```
  BUCKET_NAME=novacart-finetune
  DYNAMODB_TABLE=novacart-finetune-jobs
  SNS_TOPIC_ARN=arn:aws:sns:us-east-1:<ACCOUNT_ID>:novacart-finetune-notifications
  ```
- **Trigger:** EventBridge (see Step 6)

---

## Step 6 — Wire up EventBridge

Create a rule on the **default event bus** that fires when Bedrock changes a job's state.

1. EventBridge → Rules → Create rule → name `novacart-bedrock-job-completion`
2. Event pattern:
   ```json
   {
     "source": ["aws.bedrock"],
     "detail-type": ["Model Customization Job State Change"],
     "detail": {
       "jobStatus": ["Completed", "Failed", "Stopped"]
     }
   }
   ```

   > ⚠️ The field is `jobStatus`, **not** `status`. This exact detail caused a silent failure during development — see [`LESSONS_LEARNED.md`](LESSONS_LEARNED.md).
   >
3. Target: Lambda function → `novacart-finetune-completion`
4. EventBridge will create/attach an invoke permission automatically.

---

## Step 7 — Test the pipeline

1. Upload [`sample-data/raw_tickets_100.csv`](../sample-data/raw_tickets_100.csv) to `s3://novacart-finetune/raw/`
2. Watch CloudWatch logs for `novacart-data-prep` → it should write to `training/`, `validation/`, `metadata/`
3. `novacart-finetune-trigger` fires on the metadata write → check DynamoDB for a new job record
4. Bedrock trains (1–2 hrs). When done, EventBridge fires `novacart-finetune-completion`
5. You receive the SNS email: *"Your NovaCart model is ready!"*

---

## Step 8 — Deploy the inference API (Lambda 4 + API Gateway)

This is the *serving* side, used by the Angular chat UI. Do this after you've trained a model and created an **on-demand custom model deployment** in the Bedrock console (copy its ARN — you'll need it below).

### Lambda 4 — `novacart-inference-api`

- Code: [`src/lambdas/4_inference_api.py`](../src/lambdas/4_inference_api.py)
- Runtime: **Python 3.12** · Timeout: 30 seconds · Memory: 512 MB
- Execution role: basic execution + permission to call `bedrock:InvokeModel` / `bedrock:Converse` on your custom-model deployment ARN
- Environment variables:
  ```
  MODEL_ID=arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:custom-model-deployment/<DEPLOYMENT_ID>
  BEDROCK_REGION=us-east-1
  MAX_TOKENS=512
  TEMPERATURE=0
  ```
  > The `SYSTEM_PROMPT` is baked into the code — it must match the one used in [`1_data_prep.py`](../src/lambdas/1_data_prep.py). Override via env var only if you change it in both places.

### API Gateway (HTTP API)

1. API Gateway → Create API → **HTTP API**
2. Add an integration → **Lambda** → `novacart-inference-api`
3. Add a route → `POST /chat`
4. **Enable CORS** on the API:
   - `Access-Control-Allow-Origins`: `*` (or your frontend domain in production)
   - `Access-Control-Allow-Methods`: `POST, OPTIONS`
   - `Access-Control-Allow-Headers`: `content-type`
5. Deploy and note the invoke URL — this is what goes into [`novacart-chat/src/environments/environment.ts`](../../novacart-chat/src/environments/environment.ts) as `apiUrl`

### Quick smoke test

```bash
curl -X POST <invoke-url>/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Where do I start a return?"}'
```

You should get back a `reply` field with a NovaCart-flavoured answer.

---

## Cleanup (avoid ongoing charges)

- **Delete custom model deployments** when not testing — these incur ongoing storage cost
- Custom models themselves have a small storage cost; delete if not needed
- S3, DynamoDB, Lambda, SNS at this scale are effectively free at rest
