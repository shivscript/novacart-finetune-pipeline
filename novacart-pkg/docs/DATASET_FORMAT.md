# Dataset format

This is the format the data-prep Lambda emits and the format Bedrock expects for Nova fine-tuning. Writing it down because I burned an afternoon on the wrong format.

## What works

The format Nova wants is `bedrock-conversation-2024` — JSONL where each line is a self-contained conversation:

```json
{"schemaVersion":"bedrock-conversation-2024","system":[{"text":"You are NovaCart's friendly customer support assistant..."}],"messages":[{"role":"user","content":[{"text":"Do you accept cash on delivery?"}]},{"role":"assistant","content":[{"text":"Hi! Yes, NovaCart accepts Cash on Delivery on orders up to ₹50,000. A small ₹40 handling fee applies. — Team NovaCart 💙"}]}]}
```

Same thing expanded so it's readable (in the actual file it's all on one line):

```json
{
  "schemaVersion": "bedrock-conversation-2024",
  "system": [
    { "text": "You are NovaCart's friendly customer support assistant..." }
  ],
  "messages": [
    { "role": "user", "content": [{ "text": "Do you accept cash on delivery?" }] },
    { "role": "assistant", "content": [{ "text": "Hi! Yes, NovaCart accepts Cash on Delivery on orders up to ₹50,000..." }] }
  ]
}
```

## What doesn't work

The old `{"prompt": ..., "completion": ...}` shape from the Titan/OpenAI-style days:

```json
{"prompt": "Do you accept cash on delivery?", "completion": "Hi! Yes, NovaCart accepts COD..."}
```

This is what I started with. Bedrock rejects it on validation for Nova. Took me one failed job to realise the format had moved on.

## Field reference

| Field | Notes |
|---|---|
| `schemaVersion` | Must be exactly `"bedrock-conversation-2024"` |
| `system` | Array of `{text: ...}`. Optional but you almost certainly want it — see below. |
| `messages` | Alternating turns. Starts with `user`, ends with `assistant`. |
| `messages[].role` | `"user"` or `"assistant"` |
| `messages[].content` | Array of `{text: ...}` blocks. Nova supports multimodal; this project is text only. |

## Why the system prompt is in every line

Every training example carries the same `system` block, on purpose. The model learns to associate that framing with the response style. At inference time you have to supply the *same* system prompt or the model drifts back toward generic base behaviour.

This is the single most common reason people think their fine-tune "didn't work" — they trained with a system prompt and called the deployed model without one. Lesson 5 in `LESSONS_LEARNED.md` has the gory details.

That's also why Lambda 4 hard-codes the system prompt instead of letting the client send it.

## Train/validation split

The data-prep Lambda does an 80/20 split *after* dedup and validation, so both files are clean. The validation file is never used for weight updates — it's only there so Bedrock can report a separate validation loss curve and you can see if the model is overfitting.

## Source CSV

The raw input is a two-column CSV (see [`sample-data/raw_tickets_100.csv`](../sample-data/raw_tickets_100.csv)):

```csv
customer_query,support_response
"Do you accept cash on delivery?","Hi! Yes, NovaCart accepts Cash on Delivery on orders up to ₹50,000..."
"How do I return my order?","Hi! Returns are easy at NovaCart. You have 7 days from delivery..."
```

Each row becomes one JSON line with the shared system prompt injected. The transform lives in [`src/lambdas/1_data_prep.py`](../src/lambdas/1_data_prep.py).

## What the Lambda rejects

The data-prep Lambda returns a 400 (no retry) for any of these — see Lesson 1 in `LESSONS_LEARNED.md` for why "return, don't raise" matters here:

- Missing `customer_query` or `support_response` columns
- Empty fields
- Duplicates (case-insensitive on the query)
- Fewer than `MIN_TOTAL_EXAMPLES` valid rows after cleaning

## Dataset size, rough guidance

| Examples | What I observed |
|---|---|
| 50–100 | Enough to prove the pipeline works. Style is detectable but weak. |
| 500–1,000 | Tone and structure are reliably adopted. |
| 5,000+ | I didn't go this far. Reportedly where you start getting better recall. |

This project shipped with 80 training examples on purpose — enough to validate the wiring without spending more than a few cents on training. To make the model meaningfully better, drop a bigger CSV in `raw/` and the pipeline retrains itself.
