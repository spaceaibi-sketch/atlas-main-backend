# Atlas — SEC filing AI batched scoring

This patch changes **only** SEC filing Item AI scoring in `main_back_end/ai/`.
It introduces a reusable eight-field score validator in `shared/ai_scoring.py`
and new SEC-specific tests. It does not change News AI, Android, SQL schema, or existing scores.

## Install

Stop concurrent SEC AI jobs before applying this patch.

```bash
cd /opt/atlas
unzip -o atlas_sec_ai_batch_scoring_patch.zip
cd /opt/atlas/main_back_end
python3 -m pytest -q tests/test_sec_ai_batch_requests.py tests/test_sec_ai_stale_form_state.py
```

## Run

Default remains a single Item per OpenAI request for backward compatibility:

```bash
python3 -m ai.cli --limit 25
```

Opt in to batching, for example 5 SEC Items per API call:

```bash
python3 -m ai.cli --limit 100 --batch-size 5
```

Allow up to 5 Items per request including retrying previously failed Items:

```bash
python3 -m ai.cli --limit 100 --batch-size 5 --retry-errors
```

Preview the eligible count without calling AI or modifying SQLite:

```bash
python3 -m ai.cli --dry-run --limit 100 --batch-size 5
```

`--limit` refers to Items, **not** API requests. `--batch-size` accepts integers
from 1 to 25. The default is 1 so existing cron scripts retain their behavior.
The request uses a 300-second timeout and disables OpenAI SDK retries.
For very long Item texts, use a smaller batch size to reduce response size.

## What changes

- Each Item in a batch is sent with its **integer database `item_id`**, company,
  filing context, and `item_text`. The batch response is a JSON `results` array;
  every result must retain the correct `item_id` and the same eight scoring fields.
- Each returned Item score is independently checked for required fields,
  finite numeric values, permitted ranges, and verbatim quoted evidence.
- Output order does not matter. Unknown IDs are ignored and logged. Duplicate
  scores for one ID are rejected for that Item. Missing or malformed scores
  are marked `error` individually and are retried with `--retry-errors`.
- Valid results from the same request are committed per Item, without losing
  progress because of an unrelated invalid score.
- If the entire batch response cannot be parsed, the worker tries individual
  paid requests to salvage the Items. Account/quota failures stop further API
  calls and leave not-yet-attempted Items pending.
- Before each write, SQL checks the original Item status and Item text, and
  requires the owning filing to remain `parsed`. Stale results are discarded.
- Previously `analysed` Items selected for `--reanalyse-zero-prices` keep their
  stored assessments on failure.
- SEC Item status remains `processed -> analysed`, `processed -> error`, or
  `processed -> rejected` (empty Item). Filings retain their existing statuses.

## Validation performed

Full `main_back_end/tests` suite: **164 passed** in the offline development
workspace. Tests include mixed successful/invalid response batches, missing and
unknown IDs, duplicate scores, out-of-order results, invalid quotations,
malformed-batch singleton fallback, status changes during API calls, retries,
and previously analysed Item protection.

No live OpenAI request or production database operation was performed.
