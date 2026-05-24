"""API Gateway-triggered Lambda. POST /chat → Bedrock Converse API on the fine-tuned NovaCart model.

The system prompt is baked in here, not sent by the client — the model was fine-tuned with this exact framing, so every request needs to use it.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

# On-demand deployment ARN of the custom model.
# Bedrock console → Custom model on-demand → copy ARN.
MODEL_ID = os.environ["MODEL_ID"]
REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "512"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0"))

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are NovaCart's friendly customer support assistant. "
    "Respond in a warm, professional tone. Use numbered steps when helpful. "
    "Always close with '— Team NovaCart \U0001F499'."
)

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
    "Content-Type": "application/json",
}


def _response(status, body):
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(body)}


def lambda_handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod"))
    if method == "OPTIONS":
        return _response(200, {"ok": True})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})

    user_message = (body.get("message") or "").strip()
    if not user_message:
        return _response(400, {"error": "Field 'message' is required"})

    history = body.get("history", [])

    messages = []
    for turn in history:
        role = turn.get("role")
        text = (turn.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": [{"text": text}]})
    messages.append({"role": "user", "content": [{"text": user_message}]})

    try:
        resp = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
        )
        reply = resp["output"]["message"]["content"][0]["text"]
        usage = resp.get("usage", {})
        return _response(200, {
            "reply": reply,
            "usage": {
                "inputTokens": usage.get("inputTokens"),
                "outputTokens": usage.get("outputTokens"),
            },
        })
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        print(f"Bedrock error: {code} - {e}")
        if code in ("ThrottlingException", "ServiceQuotaExceededException"):
            return _response(429, {"error": "Model is busy, please retry shortly."})
        return _response(502, {"error": f"Model invocation failed: {code}"})
    except Exception as e:
        print(f"Unexpected error: {e}")
        return _response(500, {"error": "Internal server error"})
