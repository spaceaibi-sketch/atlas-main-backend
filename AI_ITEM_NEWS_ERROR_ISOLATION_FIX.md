# Independent AI scoring: SEC Items and news relations

This update repairs a head-of-line blocking problem in SEC Item scoring and
strengthens failure isolation in news relation scoring. It applies to the
canonical launchers and their legacy compatibility launchers without command
changes.

## SEC: `ai/worker.py`

- Parse and validate batch results per requested Item ID. Commit all valid
  results in independent SQLite transactions *before* considering repair calls.
- Queue items with nonverbatim evidence and perform each item-specific quote
  repair **only after all normal batches have been processed**. A single slow
  quote repair cannot delay subsequent batches. Quote verification remains
  strict and unrepaired quotes never produce a saved score.
- On an unparseable batch response, request items individually and commit
  each successful result immediately, before contacting the next Item.
- A local SQLite failure on one Item is logged and leaves it pending, without
  rolling back or preventing another Item's successful commit.
- On an unattributable request/network error, retain pending statuses for the
  affected batch and proceed to the next batch. On account-wide authentication,
  permission or quota failure, stop paid requests and leave unattempted work
  pending instead of marking it as item-specific errors.
- Preserve guarded updates so changes to filing status, item content, or item
  status during inference cannot overwrite newer records.

## News Phase 2: `news_pipeline/scoring.py`

- Save each valid relation and its parent-news status in an individual atomic
  transaction. Other relations remain committed if one write fails.
- Save all good scores before logging invalid or missing decisions. Invalid
  or missing relations remain eligible for later attempts.
- If a complete news batch is unusable, make individual fallback requests and
  save each successful relation immediately, even if a later request fails.
- Keep the parent news record `connected` until **every** associated relation
  has been scored successfully.
- Emit per-relation `NEWS ANALYSED` console logs as commits occur.

## Verification

Run from `main_back_end`:

```bash
PYTHONPATH=. python -m pytest -q
```

Dedicated failure-isolation regression tests live in
`tests/test_ai_item_error_isolation.py`. These use simulated Responses API
clients and SQLite databases, including live commit-order assertions, malformed
API responses, per-record SQL errors, and a failure in a prior request batch.
No live OpenAI request or production database was used for verification.

**Operational detail:** An API call that itself contains multiple Items is
still one synchronous HTTP request; no application can save that call's model
results *before the API sends them*. This fix removes avoidable waiting on
item-specific repairs *after* the initial batch response. Account-wide outages
and the intrinsic duration of an in-flight shared API call are not caused by a
single Item and remain the only request-level limits.
