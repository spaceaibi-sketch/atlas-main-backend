# Atlas News Phase 2 + SEC Item AI Analysis Audit / Fix

Base: latest `atlas_news_phase1_partial_batch_catalog_identity_patch.zip` applied to supplied `main_back_end(2).zip` baseline. This overlay touches **only `main_back_end`**. It does not change News Phase 1 extraction, the official company catalog, deterministic identity matching, Android backend, Android app, SQL table definitions, or existing AI scores.

## Confirmed issues repaired

1. **News Phase 2 used the removed `news.batch_status`** in pending selection, score commits, and status updates. Both the `--dry-run` and actual scoring could fail. Phase 2 now selects live `news.status` and `news_company_relations.status` values.
2. **One invalid News relation score blocked the entire Phase 2 batch.** Now valid scores save, while unknown, missing, duplicate, and invalid scores are logged and left `connected` for retry. No missing decision becomes an invented neutral assessment. A structurally unusable JSON response leaves the affected request batch pending.
3. **One final pending News relation could be deferred indefinitely.** Now it is processed in a final one-relation request, if needed.
4. **News parents could be marked `analysed` incorrectly or remain stale.** Every saved relation is marked `analysed` only with validated fields. Parent News is `analysed` only if **every** relation on that story is analysed; otherwise it stays `connected`. The query also detects relations added to previously analysed News.
5. **News aggregate calculations hid completed relations while other relations on the same story were pending.** Aggregate SQL now includes `r.status='analysed'` with News in `connected` or `analysed` status; only completed scores contribute.
6. **SEC Item reanalysis could overwrite the status of an earlier valid result with `error` on a later AI failure.** Earlier analysed scores and their `analysed` status now survive failed reanalysis. Other SEC items continue independently.
7. **SEC Item queue could include items from forms not in `parsed` state.** It now only selects parsed-form items and guards save with a still-parsed-form condition.
8. **Dashboard and reporting paths still queried `batch_status`.** The quality view, article detail, main-backend mobile queries, and migration summary now work against the new `news.status` schema. Event grouping dashboard status panels (removed from schema) are retired; the five current News statuses are reported.
9. **AI aggregate refresh failures after successful score commits** are logged and counted separately, preserving saved scores.

## Response and retries

- Phase 1 retains its existing 300-second single-attempt timeout.
- Both Phase 2 News and SEC Item scoring now use **300-second single attempts**, with automatic SDK retries disabled to bound each request.
- News Phase 2 relation-level failures remain `connected` and are eligible on the next run. No whole-batch rollback merely due to one invalid score.
- SEC Item request errors remain `error` and must be retried with `--retry-errors` (existing SEC semantics). Empty item text is `rejected` and is not sent to AI.
- Whole-request errors (network / unparseable JSON / API refusal) leave News Phase 2 candidates pending; authentication/rate-limit errors stop issuing further requests during that run.
- No code can guarantee that a future AI response or external outage will be error-free.

## Install

Stop AI jobs before updating code. Back up `/opt/atlas/data/edgar.sqlite3` first.

```bash
cd /opt/atlas
unzip -o atlas_news_sec_ai_scoring_status_audit_patch.zip
cd main_back_end
PYTHONPATH=. python3 -m pytest -q tests
```

**Read-only status audit (recommended first):**

```bash
cd /opt/atlas/main_back_end
python3 -m scripts.audit_ai_status --db /opt/atlas/data/edgar.sqlite3
```

The audit prints counts for News, relationships, forms and SEC items, pending queues and exact state inconsistencies. It opens SQLite in read-only mode and makes **no** API calls or database changes. Exit code 2 means anomalies were detected and should be reviewed; exit code 0 means none of the included invariant checks found an issue.

**Check work waiting for AI (the scoring CLI may call schema initialization):**

```bash
python3 -m news_pipeline.scoring_cli --dry-run --limit 0
python3 -m ai.cli --dry-run --limit 0
```

**Run Phase 2:**

```bash
python3 -m news_pipeline.scoring_cli --limit 100 --batch-size 20
python3 -m ai.cli --limit 25 --retry-errors
```

Each API call may take up to five minutes. `--retry-errors` is required for previously failed SEC Items. No OpenAI calls were made in patch verification.

## Normal statuses

| Pipeline | Queue status | Success | Failure / other |
|---|---|---|---|
| News Phase 1 | `created` | `extracted`, or `connected` after unique identity matching | `skipped` for NONE/duplicates; malformed article stays `created` |
| News Phase 2 relation | `connected` | `analysed` | Remains `connected` for automatic next-run retry |
| News Phase 2 article | `connected` | `analysed` only when **all** its relations complete | Stays `connected` if any relation still pending |
| SEC filing | `created` | `extracted` → `parsed` | `error` on form ingestion/parse failure |
| SEC Item AI | `processed` | `analysed` | `error` (use `--retry-errors`), or `rejected` if empty |

## Verification

`PYTHONPATH=. pytest -q tests` → **150 passed** locally, including mocked mixed valid/corrupt News scores, duplicate response IDs, missing scores, singleton queue, old valid SEC scores surviving reanalysis errors, retry behavior, aggregate consistency, and read-only status audit.

**Limits:** These are offline tests against temporary SQLite databases, not a reproduction with your production DB or real OpenAI Responses API calls. This patch does not fix the intentional Phase 1 last-single-article deferral (batch-only extraction policy), and it does not repair preexisting database anomalies automatically; use the audit report to identify them.
