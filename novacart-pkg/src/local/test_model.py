"""
Test a NovaCart fine-tuned Nova model via an on-demand custom model deployment.

Usage:
    python test_model.py --deployment novacart-nova-ondemand
    python test_model.py --deployment novacart-nova-ondemand --prompts my_prompts.txt
    python test_model.py --deployment-arn arn:aws:bedrock:us-east-1:...:custom-model-deployment/v3vjdfhtdtg1

The system prompt MUST match the one used during training (see 1_data_prep.py SYSTEM_PROMPT).
"""

import argparse
import os
import sys

import boto3

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are NovaCart's friendly customer support assistant. "
    "Respond in a warm, professional tone. Use numbered steps when helpful. "
    "Always close with '— Team NovaCart 💙'.",
)

DEFAULT_PROMPTS = [
    "Where do I start a return?",
    "It's been 10 days, can I still return my running shoes?",
    "How do I track my order?",
    "I was charged twice for the same order, please help.",
    "Can I change the delivery address after placing the order?",
    "How do I cancel my subscription?",
    "My package arrived damaged.",
    "Do you ship to Bhutan?",
]


def resolve_deployment_arn(bedrock, name: str) -> str:
    resp = bedrock.list_custom_model_deployments()
    for d in resp.get("modelDeploymentSummaries", []):
        if d["customModelDeploymentName"] == name:
            return d["customModelDeploymentArn"]
    raise SystemExit(f"No active deployment named {name!r}")


def invoke(runtime, model_id: str, user_text: str) -> str:
    resp = runtime.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        inferenceConfig={"temperature": 0.0, "maxTokens": 400, "topP": 1.0},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--deployment", help="Custom model deployment NAME")
    g.add_argument("--deployment-arn", help="Custom model deployment ARN")
    ap.add_argument("--prompts", help="Optional file with one prompt per line")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    bedrock = session.client("bedrock")
    runtime = session.client("bedrock-runtime")

    deployment_arn = args.deployment_arn or resolve_deployment_arn(bedrock, args.deployment)
    print(f"Deployment: {deployment_arn}\n")

    if args.prompts:
        with open(args.prompts, encoding="utf-8") as f:
            prompts = [ln.strip() for ln in f if ln.strip()]
    else:
        prompts = DEFAULT_PROMPTS

    sign_off = "— Team NovaCart"
    sign_off_hits = 0

    for i, q in enumerate(prompts, 1):
        print(f"=== {i}/{len(prompts)} ===")
        print(f"Q: {q}")
        try:
            a = invoke(runtime, deployment_arn, q)
        except Exception as e:
            print(f"ERROR: {e}\n")
            continue
        print(f"A: {a}")
        if sign_off in a:
            sign_off_hits += 1
        print()

    print(f"Brand sign-off coverage: {sign_off_hits}/{len(prompts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
