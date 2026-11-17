# Atlas final five-table cleanup — offline, data-preserving

**Source inventory:** `/opt/atlas/data/edgar.sqlite3` (2026-10-24).

This tool creates a **new** cleaned SQLite database preserving exactly five
application tables:

- `companies` — identity and currently stored rolling weighted company metrics
- `raw_items` — News, SEC filings, SEC items, full content, source metadata, one status
- `relationships` — company/source associations, AI results, cumulative impact and monetary effects
- `summary` — company aggregate, freshness and price-summary values
- `price_bars` — full 15-minute historical OHLCV bars

It deletes these tables **in the cleaned copy only**: `news`, `forms`, `items`,
`news_company_relations`. `sqlite_sequence` can remain because it is an internal
SQLite object needed for `AUTOINCREMENT`.

It removes precisely two demonstrably migration-only relationship columns:
`legacy_relation_id` and `legacy_item_id`. **Their non-null values are first
preserved** in `relationships.metadata_json` as `archived_legacy_relation_id`
and `archived_legacy_item_id`. All other columns remain because they either
represent business data or have not yet been proven unused by every consumer.

## Why not drop the tables from the active database now?

Your own legacy-cleanup audit reported production Python readers and writers still
querying `news`, `forms`, `items`, and `news_company_relations`, including News
collectors, SEC extraction, AI scoring and Dash pages. The unified migration's
sync triggers are presently keeping these old-source writers compatible with
new data. Immediately dropping their input tables **breaks data ingestion**.

The script therefore supports `--build` to create a verified clean copy without
risking the active database. A separate `--activate` mode is **guarded**, checks
production source files for legacy SQL and requires the user to confirm all
database clients are converted and stopped. It will currently refuse cutover
until the main backend conversion is complete. A clean Java read API alone is
not sufficient.

## Deployment — run from the main backend server

Copy `scripts/atlas_five_table_migration.py` to
`/opt/atlas/main_back_end/scripts/atlas_five_table_migration.py`.

1. Inspect free disk space: cleanup involves an SQLite snapshot and VACUUM.

```bash
df -h /opt/atlas/data
cd /opt/atlas/main_back_end
```

2. Read-only audit:

```bash
python3 scripts/atlas_five_table_migration.py \
  --db /opt/atlas/data/edgar.sqlite3 --audit
```

3. **Create cleaned database next to the live one (safe):**

```bash
python3 scripts/atlas_five_table_migration.py \
  --db /opt/atlas/data/edgar.sqlite3 --build
```

Default output: `/opt/atlas/data/edgar.five-table.sqlite3`.
This file is created only if absent. If it exists already, choose another path
using `--clean-db` (the tool will not overwrite an existing file). The
original `/opt/atlas/data/edgar.sqlite3` is **unchanged**.

4. Verify the clean copy:

```bash
python3 scripts/atlas_five_table_migration.py \
  --db /opt/atlas/data/edgar.sqlite3 \
  --clean-db /opt/atlas/data/edgar.five-table.sqlite3 --verify
```

5. Check the cutover blocker:

```bash
python3 scripts/atlas_five_table_migration.py \
  --db /opt/atlas/data/edgar.sqlite3 \
  --source-root /opt/atlas/main_back_end --can-cutover
```

**Expected CURRENT result:** `CUTOVER BLOCKED`, listing the Python modules
whose SQL still references the old tables. Do not rename/copy the clean DB over
the live one to bypass this check.

6. **ONLY AFTER** all collectors, schedulers, scoring workers, Dash and any
other consumers have been converted to canonical tables and stopped: run the
explicit guarded cutover. It builds a fresh snapshot rather than promoting an
old, potentially stale clean copy, keeps the full original under a timestamped
name for rollback, and checks open database file handles with Linux `fuser`.

```bash
python3 scripts/atlas_five_table_migration.py \
  --db /opt/atlas/data/edgar.sqlite3 \
  --source-root /opt/atlas/main_back_end \
  --activate --confirm-cutover
```

This mode is **not appropriate for today's legacy-writing main backend**. A
static source scanner cannot prove compatibility for external processes, so
run the full News/SEC ingestion, Dash, Java API and price regression tests on
the clean copy before a production cutover.

## Database integrity checks

- Full `PRAGMA integrity_check` and `PRAGMA foreign_key_check` before and after.
- Source key and full-text parity from News, forms and items to `raw_items`.
- Relationship identity parity from News and SEC sources to `relationships`.
- Filing-to-company relation preservation.
- Counts of *all five kept tables*, including every row of `price_bars`.
- All 37 known triggers / 43 indexes from the uploaded schema tested against a
  fixture; old summary/ingestion triggers are dropped, canonical impact and
  price refresh triggers retained. New company summary rows still created.
- Optional `--no-vacuum` skips physical compaction of the staged output.

## Important scope limitations

This script is a **database cleanup and migration**, not a replacement for the
old Python writers. Main backend writers and dashboard queries must be ported
before cutover. It does not rewrite their source code or delete source files.
Values such as historical prices and seven-day weighted metrics are preserved,
not recalculated. Additional columns should only be removed once all readers
and writers are proven not to use them.
