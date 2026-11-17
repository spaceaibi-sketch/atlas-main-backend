# Atlas mobile News / SEC summary freshness fix — 2026-10-25

## Confirmed issue in uploaded backend

- Java API reads the canonical `summary` table from the five-table SQLite database.
- The installed three `atlas_mob_price_*` triggers refresh price data, not News/SEC data.
- News/SEC ingestion could commit `raw_items` / `relationships` while the directory
  still displayed older `summary` values. The prior automatic refresh was mostly
  after successfully committed AI scoring, not after news or filings ingestion.
- The previous summary code used compatibility `atlas_articles` / `atlas_links`
  projections and counted some skipped/inactive rows inconsistently.

## Included changes

- `dashboard/mobile_summary.py`: set-based recomputation **from canonical tables**,
  using `relationships` joined to `raw_items`; active linked news dates,
  SEC filing/item dates and stored (non-skipped) assessed impacts. No generated
  impacts. Also a `--events-only` switch that keeps independent price columns.
- `extract_news.sh`, `extract_recent_filings.sh`, `extract_forms_items.sh`:
  recompute News/SEC materialization after the extraction process, **including
  partial failures** where data had already been committed. Preserve the
  original job's nonzero exit code. Skip summary mutations on `--dry-run` and
  `--help`.
- `scripts/repair_mobile_summary.py`: read-only full-data audit by default;
  `--apply` makes an online SQLite backup (including WAL), integrity-checks it,
  and rebuilds News/SEC cache for every company.
- `tests/test_mobile_summary_freshness.py`: regression cases.

## Deployment

Extract the full-backend ZIP over `/opt/atlas` (it contains `main_back_end/`),
**without** deleting your existing `.env`, data files, cron configuration,
 or virtual environment. The update-only ZIP has the same root layout.

From the backend host, with the writer idle during the one-time repair:

```bash
cd /opt/atlas/main_back_end
python3 scripts/repair_mobile_summary.py --db /opt/atlas/data/edgar.sqlite3
python3 scripts/repair_mobile_summary.py --db /opt/atlas/data/edgar.sqlite3 --apply
python3 scripts/repair_mobile_summary.py --db /opt/atlas/data/edgar.sqlite3
```

`--apply` writes **only** `summary` cache fields; it does not change source
News, SEC records, relationships, assessments, prices, or AI scoring outputs.
It creates a timestamped `edgar.sqlite3.pre-mobile-summary-*.bak` using SQLite's
safe online backup API. Keep that backup until you validate the live mobile
page. The refresh is transactional. It can cause a temporary SQLite write lock;
perform the one-time repair during a quiet ingestion window.

Manual future ingests with `bash extract_news.sh`, `bash extract_forms_items.sh`,
 or `bash extract_recent_filings.sh` refresh the summary when the ingestion
 command ends. Existing `cron_job.sh` invokes these same wrappers. Confirm
 that your cron entries exist and have actually been running:

```bash
crontab -l | grep 'SEC_EDGAR_CRON:'
```

If cron is absent/disabled, the patch cannot schedule new ingests. The Java
 API does not write the summary; it will read new rows once refreshed.

## Validation

Focused tests passed on an isolated five-table SQLite fixture; shell wrappers
 were exercised for successful, failing, and dry-run invocations. No live
 production DB, Java service, Android APK, or device test was performed.
The uploaded backend also includes older tests referencing removed legacy
 `news` / `forms` / `items` tables, and the entire pytest suite currently
 cannot collect because of two such outdated tests. These are pre-existing
 migration/test inconsistencies, not evidence that production data was repaired.
