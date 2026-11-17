# Atlas — simplify the already-migrated unified schema

**Main backend target:** `/opt/atlas/main_back_end`

**Shared SQLite DB:** `/opt/atlas/data/edgar.sqlite3` (verify the configured path before changing anything)

This is an incremental, transactional migration for the successful `migrate_unified_model.py` installation. It is **not** the original unified transition. It needs Python's SQLite 3.35 or newer.

## New canonical contract

- `raw_items`: all News, filings, and SEC items. `status` is the only canonical workflow status, restricted to `created`, `skipped`, `connected`, `analysed`. `source_name` means the publisher for News, `SEC` for filings, and the filing type (`8-K`, `10-Q`, etc.) for SEC items. `parent_key` is kept to identify the parent filing without a parent FK. Remove `parent_source`, `analysis_scope`, and `processing_status`.
- `relationships`: source/company connection; scored assessments, evidence, monetary and forecast data; `accumulated_impact` and `accumulated_monetary`. Remove `status`, `analysed_at`, `accumulated_event_count`, `accumulated_news_count`. **An impact of `0` counts as an analyzed result; `NULL` means no saved impact.**
- `companies`: unchanged.
- `summary`: unchanged (already renamed from `company_mobile_summary`).

All filing rows remain in `raw_items` with `source_type='filing'`; SEC item rows retain their filing linkage in `metadata_json.form_id` and `parent_key`.

## Deployment order

1. Pause **all** main backend News/SEC writers, scoring jobs and cron schedules; stop the Java API:

   `sudo systemctl stop atlas-mobile-api-java`

2. Overlay this main backend ZIP onto `/opt/atlas/main_back_end` (original relative paths). Verify it is the exact SQLite file used by the running writer.
3. Run from `/opt/atlas/main_back_end`:

   ```bash
   python3 scripts/migrate_simplified_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --dry-run
   python3 tests/test_simplified_unified_model.py
   python3 scripts/migrate_simplified_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --apply
   python3 scripts/migrate_simplified_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --verify
   ```

   `--apply` uses SQLite's online backup API to write a complete, timestamped `.bak` in the same directory **before** running changes in one `BEGIN IMMEDIATE` transaction. Failure rolls back the migration. Keep all backups.

4. Overlay the Java API ZIP onto your **existing** `android_back_and_java` source directory, then deploy that source:

   ```bash
   sudo bash scripts/install-service.sh
   sudo systemctl restart atlas-mobile-api-java
   sudo systemctl status atlas-mobile-api-java --no-pager
   ```

5. Validate the Java `/api/v1/companies/<id>/timeline`, `/news`, `/sec/items`, and `/sec/forms` endpoints with your existing authentication before re-enabling News/SEC writers; then resume paused jobs.

## Safety and incomplete work

**This change does not remove `news`, `forms`, `items`, or `news_company_relations`.** Those are still written by multiple production collectors and scoring modules; synchronization triggers are reinstalled for the smaller canonical schema. Deleting them now would stop ingestion. A truly four-table-only physical database still requires replacing those writers and other dashboard readers first.

It also does not delete companies, price bars, operational logs, user files, or any News/SEC records. The migration checks source counts, foreign keys, four-state validity, filing names, and SQLite integrity. The new Java read-only API no longer needs legacy tables and will fail clearly if the simplified schema has not been deployed.

**One-status tradeoff:** for a News item associated with multiple companies, its source-wide `status` becomes `analysed` when all non-skipped company relationships are analyzed. Per-company completion is inferred from `relationships.ai_impact IS NOT NULL`, which distinguishes zero impact from not-yet-analyzed results.
