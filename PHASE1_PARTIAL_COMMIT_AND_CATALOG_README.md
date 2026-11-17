# Atlas Phase 1: partial-batch recovery and official catalog names

This is a **main_back_end-only** overlay. It does not change Android code, the mobile API, SQLite tables, or Phase 2 scoring algorithms.

## Why

Previously one malformed `ID|Company|Reason` line, lower-case company name, contradictory decision, or missing ID threw `ValueError` and rolled back all valid articles in the same AI batch. The original error message was `Company must be a properly capitalized name with a safe reason`.

## New behavior

- Phase 1 receives `company_catalog` as one `Official Company Name|Ticker` line per SEC company.
- The prompt explicitly **requires the precise official name from the catalog** whenever a mentioned/directly or indirectly relevant company can be uniquely identified there. For example, `Amazon` => `AMAZON COM INC` if the catalog contains that name. Tickers disambiguate; output never substitutes ticker for name.
- A genuine company absent from the catalog may still appear in `news.companies`, consistent with the existing requirement to retain unmatched extracted names. Only unique SEC identity matches create candidate relationships.
- There is **no capitalization check, CEO check, article mention requirement, or exposure-evidence gate before deterministic identity matching**. Those are not conditions for inserting candidate links. The old `identity_validation.validate_resolved` remains a backward-compatible pass-through. Phase 2 assesses substantive relevance.
- Format-only checks remain to identify which article a line belongs to and avoid inventing decisions. Empty names/reasons, contradictory `NONE`, repeated conflicting company lines, and omitted decisions quarantine **only their own article** (kept `created` with empty companies and no new relations).
- Other valid articles in the same batch persist normally; `ID|NONE` skips that specific article; unlisted supported companies remain `extracted`; uniquely matched names become `connected`.
- A malformed or overlapping **dedup group** also quarantines its known participating new article IDs, not unrelated articles. Fully malformed/unrecoverable whole-request JSON or API errors cannot be partitioned safely, so those requests remain pending.
- Responses lacking an attributable article ID are logged and ignored; omitted article IDs stay `created` for retry.
- New run counters: `partial_batches`, `failed_article_decisions`. A partial run can report `errors=1` and exit nonzero **while also having saved good articles**.
- The existing 300-second-per-Phase-1-AI-call timeout and timezone-aware UTC pipeline remain unchanged.

## Apply

Stop the main news collector, back up `/opt/atlas/data/edgar.sqlite3`, then from the Atlas project root:

```bash
cd /opt/atlas
unzip -o atlas_news_phase1_partial_batch_catalog_identity_patch.zip
cd main_back_end
python3 -m pytest -q tests/test_phase1_partial_commit_catalog_prompt.py tests/test_ai_first_correlation.py tests/test_phase1_resilience_utc.py tests/test_phase1_catalog_partial_recovery.py tests/test_news_phase1_timeout.py tests/test_bidirectional_sec_name_contains.py
python3 -m news_pipeline.cli --ai-only
```

If just **one** article remains queued and there is no recent reference/main article, the existing batch-only logic may defer it until another article arrives. It is not skipped or forgotten. The 3-hour rule applies at ingestion, not retries.

## Verification limits

60 focused tests passed in the supplied source snapshot, including simulated mixed good/corrupted OpenAI responses. No live OpenAI or production DB test was performed.

The unmodified source snapshot also contains unrelated `batch_status` references in `dashboard/data_quality.py`, `dashboard/mobile_queries.py`, and `news_pipeline/scoring.py` even though the news schema removed that field. The **full** legacy test suite therefore has 7 pre-existing incompatibility failures; this overlay intentionally does not edit those unrelated components. Check the installed version of those files separately if you encounter errors there.
