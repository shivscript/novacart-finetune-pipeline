# Lessons Learned — the AWS side

The modelling lessons live in the root [README](../../README.md). This file is for the *infrastructure* war stories — the silent failures, the wrong field names, the retry behaviour I didn't know about until it bit me. The kind of things you only learn by building the thing and watching it break.

---

## The Lambda that ran three times when I only wanted it to run once

The first time I fed the data-prep Lambda a malformed CSV, it ran three times before giving up. No code change, no manual retry — just three identical failures in the logs.

Turns out S3-triggered Lambdas are invoked **asynchronously**, and AWS will automatically retry an async invocation twice more if the function raises an exception. Which makes sense for transient failures (a downstream service blipped, try again) but is completely wrong for permanent ones (the data is bad and will never not be bad).

The fix was to stop raising. For validation errors — anything that's structurally hopeless — the Lambda now *returns* a 400-shaped response. AWS sees a clean return and marks the invocation done. Genuine transient errors (a Bedrock throttle, an S3 hiccup) still raise so the retry actually helps.

What I took from this: in event-driven AWS, "raise on error" isn't a default — it's a choice with consequences. Know your invocation model before you decide how to fail.

---

## My base model got retired mid-build, and the new one wanted a different data format

I started this project on Amazon Titan. Halfway through, Titan was deprecated for fine-tuning. The fine-tune job just stopped accepting it, with the kind of error message that doesn't immediately tell you "this model no longer exists for this purpose."

Pivoting to Amazon Nova Micro wasn't a one-line change. Titan accepted the old `{"prompt": ..., "completion": ...}` JSONL format. Nova rejects it — it wants the **Bedrock Conversation format**: a `schemaVersion`, a `system` array, and `messages` tagged with roles. So the data-prep Lambda's whole conversion step had to be rewritten. The full spec is in [`DATASET_FORMAT.md`](DATASET_FORMAT.md).

Two things stuck with me. First, the base model is *configuration*, not a constant — I now treat the model ID as an env var and assume it'll change. Second, switching base models can mean re-engineering your data pipeline, not just swapping an identifier. The format is part of the contract.

---

## The completion Lambda never ran — and there were no errors anywhere

This one took me a while to find. Fine-tuning jobs were completing successfully, I could see them in the Bedrock console, but the completion Lambda never fired. No CloudWatch errors. No EventBridge rule failures. Just… nothing.

I'd written the EventBridge pattern matching on `detail.status`. Bedrock actually emits the field as `detail.jobStatus`. A non-matching pattern doesn't throw — it silently fails to match, the rule does nothing, and you have zero signal that anything is wrong. The only way I caught it was checking the actual event payload in CloudWatch instead of trusting my assumed field name.

The corrected pattern:

```json
{
  "source": ["aws.bedrock"],
  "detail-type": ["Model Customization Job State Change"],
  "detail": { "jobStatus": ["Completed", "Failed", "Stopped"] }
}
```

The takeaway is bigger than this one bug: **silent failures are the worst kind**. When an event-driven component "does nothing," your instinct should be to verify the exact event schema at the source, not to debug the consumer. One wrong word in a JSON path can be invisible.

---

## EventBridge can call your Lambda only if your Lambda lets it

Later in the project I hit the same "no email arrived" symptom *again* — but this time the field name was right. The job completed, the EventBridge rule matched (I could see it in the rule's metrics), and still nothing happened on the Lambda side.

What I'd missed: an EventBridge rule needs a **resource-based policy** on the Lambda granting `lambda:InvokeFunction` to the EventBridge principal. When you wire the rule up through the console, AWS adds this for you. When you wire it up via CLI or Terraform — or if the policy ever gets removed — the rule will appear to match but the invocation gets dropped. The only breadcrumb is a `FailedInvocations` count on the rule's CloudWatch metrics, which I happened to spot while looking for something else.

The fix is one CLI call:

```bash
aws lambda add-permission \
  --function-name novacart-finetune-completion \
  --statement-id eventbridge-invoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn <event-rule-arn>
```

Lesson: when one AWS service "calls" another, two permissions are always in play — the caller's IAM role *and* the callee's resource policy. Missing either side fails silently. Always check both.

---

## Summary

| # | War story                                                                 | What it taught me                                            |
| - | ------------------------------------------------------------------------- | ------------------------------------------------------------ |
| 1 | Lambda ran three times on bad input                                       | Async invocations retry on raise — return instead            |
| 2 | Base model got deprecated, new one wanted a new data format               | Treat model ID as config; data format is part of the contract |
| 3 | EventBridge rule never fired (`status` vs `jobStatus`)                    | Verify exact event schemas; silent matches are silent failures |
| 4 | EventBridge matched the rule but never invoked the Lambda                  | Caller IAM *and* callee resource policy — both must be right |
