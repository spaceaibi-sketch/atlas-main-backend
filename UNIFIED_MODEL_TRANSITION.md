# Atlas unified domain model — safe data-preserving transition

## Scope

The canonical **business-domain** tables are:

1. `companies` — company identity and existing company attributes.
2. `raw_items` — all three source types (`news`, `filing`, `item`) in the same table.
   `parent_source` stores `8-K`, `10-Q`, `10-K` on SEC rows; `parent_key` stores
   the filing's original source URL, and `source_key` identifies the source row.
   `analysis_scope='children'` on filings prevents analyzing a whole form and
   scoring its constituent SEC items a second time; News and SEC items use `self`.
3. `relationships` — source-to-entity/company connection, shared final-analysis
   status (`connected`, `queued`, `analysing`, `analysed`, `retry`, `failed`,
   `skipped`), company-specific impact, confidence, reasons, money, forecast,
   historical accumulated impact, cumulative money and event counts.
4. `summary` — materialized company aggregates, renamed from
   `company_mobile_summary` **without losing rows**.

`price_bars` and operational storage remain intact. They are not News/SEC
business tables and are still needed for price history and ingestion. Other
legacy tables currently remain as transitional staging (see below).

## How data is preserved

- Copies `news` into `raw_items(source_type='news')`.
- Copies **every** row of `forms` into `raw_items(source_type='filing')`. A filing
  is not discarded just because it has child items.
- Copies **every** row of `items` into `raw_items(source_type='item')`.
- Copies all News company/entity relationships and SEC item assessments into
  `relationships`. A filing also has a company relationship but is not
  considered assessed when its SEC items supply the scored evidence.
- Keeps source-specific attributes in `metadata_json`. Long `article_text` and
  `item_text` are held in the generic `content` field, not duplicated in JSON.
- Preserves original IDs in source_key, legacy_relation_id and legacy_item_id.
- Reconstructs accumulated impact using one deterministic chronological sequence
  per company across assessed News and SEC items (zero is a valid impact).
- Moves all existing company summary values to `summary`, then recalculates and
  reinstalls the source-owned summary update triggers.
- Copies are done with set-based SQLite SQL rather than Python per-row loops.

## Deployment and migration

**Do not deploy this over a running writer.** In a maintenance window:

1. Stop or pause all main-backend News, SEC and price ingestion and the Java API.
2. Copy the MAIN update-only ZIP over your existing `main_back_end` tree.
3. Identify the exact `sec.db.path` in the Java API config; it must point to the
   same SQLite DB that the main backend writes.
4. Preview the database:

   ```bash
   python3 scripts/migrate_unified_model.py --db /path/to/atlas.sqlite3 --dry-run
   ```

5. Apply the migration:

   ```bash
   python3 scripts/migrate_unified_model.py --db /path/to/atlas.sqlite3 --apply
   ```

   The script makes a full, timestamped SQLite-online backup in the **same
   directory as the live DB**, verifies it, acquires an IMMEDIATE write lock,
   creates/renames data and triggers in one transaction, verifies source-to-
   destination row counts, then commits. On error it rolls back automatically.

6. Verify:

   ```bash
   python3 scripts/migrate_unified_model.py --db /path/to/atlas.sqlite3 --verify
   ```

7. Copy the JAVA update ZIP over `android_back_and_java`, rebuild/reinstall the
   Java backend with its existing `sudo bash scripts/install-service.sh`, then
   `sudo systemctl restart atlas-mobile-api-java`.
8. Restart main-backend writers. Recheck the Java company timeline and News/SEC
   detail requests before reopening all ingestion schedules.

If deploying main-backend code before running the migration, do not restart
writers yet: `summary` and `company_mobile_summary` are **different names**.

## What is and is not removed

- The update-only ZIPs contain only changed source/config/test files. No unrelated
  source, binaries or APK files are distributed.
- **No user records, source files or legacy tables are dropped.** This is
  intentional: current main-backend ingestion/scoring workers still read/write
  `news`, `forms`, `items` and `news_company_relations`. SQLite triggers now
  mirror their inserts/updates/deletes into the canonical four-table model.
- The Java reader has been switched to the new canonical domain tables.
- Redundant old `atlas_cumulative_%` triggers are removed after the new
  relationship history is populated and checked. Their existing source columns
  are preserved for forensic compatibility but no longer maintained; the
  `relationships` columns are authoritative.
- A truly **four-table-only physical database** requires porting the original
  ingestion writers and dashboard/admin SQL to target `raw_items` and
  `relationships` directly, then verifying every workflow before retiring
  the staging tables. **This transition does not perform that destructive
  final cut-over**; do not manually DROP or remove them yet.
- The full DB `.bak` is retained for rollback; the script does not move/delete
  live attachments or archived SEC filing files.
- This unifies storage and the downstream read model, **not the original
  News/SEC AI worker orchestration yet**. The existing workers continue to
  score in their source-specific pipelines, with their results mirrored to the
  shared relation/status schema. Switching the workers to a single queue still
  requires a dedicated pipeline refactor.

## Recovery

With all writers and Java stopped, restore the timestamped `.bak` to the same
SQLite path using `sqlite3` online backup (or a verified file replacement while
no SQLite connections or WAL writers exist), roll back **both** source overlays,
then restart services. Never overwrite a live SQLite DB while writers are active.
