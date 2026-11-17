# Atlas offline five-table database stage and archival cutover

**Read before running.** This is a *database-only archival cutover*, not a finished migration of every Python collector/dashboard SQL query. It preserves original historical records in a **complete external SQLite backup** but DOES NOT migrate unmapped/different legacy rows into the five active tables. Your last audit found 237 news keys, 1,542 mismatched item texts, 583 filing/company associations and 60 relations needing reconciliation. After activation, the old Python collectors and Dash backend may **fail** until ported; leave them stopped. The Java Android API reads the five canonical tables, but this has **not** been live-tested with your production database. Do not mistake an archived-history cleanup for a fully lossless live five-table conversion.

**This utility does not modify `backend.sh`, Android, Java, `.env`, `atlas.properties` or cron.** Only one new Python script is added in its source-relative location.

## Stop all Atlas database clients (production server, root)

```bash
cd /opt/atlas
# Remove only Atlas-owned root-crontab jobs (save root's cron first).
crontab -l > /opt/atlas/tmp/root-crontab.before-atlas-db.txt || true
bash backend.sh cron-remove
systemctl stop atlas-mobile-api-java.service atlas-python-dash.service
# Verify no other collectors/processes are using SQLite before any database operation.
fuser -v /opt/atlas/data/edgar.sqlite3 || true
```

Wait for any already-running collector process to exit. `fuser` must show **no** process holding the database. Ensure substantial free disk space under `/opt/atlas/tmp` and the `/opt/atlas/data` filesystem; staging keeps two copies plus a possible temporary third copy at activation.

If `-wal` or `-shm` sidecars remain nonempty after all clients stop, checkpoint using the installed `sqlite3` CLI **before staging**, then make sure all clients remain stopped:

```bash
sqlite3 /opt/atlas/data/edgar.sqlite3 'PRAGMA wal_checkpoint(TRUNCATE);'
```

## Step 1 — Stage, without modifying the live database

```bash
cd /opt/atlas
python3 main_back_end/scripts/atlas_offline_five_table.py stage \
  --db /opt/atlas/data/edgar.sqlite3 \
  --out-dir /opt/atlas/tmp/database \
  --source-root /opt/atlas/main_back_end
```

This creates three files, printed on screen:
* `edgar.original.TIMESTAMP.sqlite3`: complete read-consistent backup including ALL legacy tables and data, mode `0600`.
* `edgar.five-tables.TIMESTAMP.stage.sqlite3`: independently checked copy with exactly the five application tables; `legacy_item_id` and `legacy_relation_id` are copied to surviving `relationships.metadata_json` before those columns are dropped.
* `edgar.five-tables.TIMESTAMP.json`: report of source/stage SHA-256s, all row counts, coverage gaps and legacy backend dependencies.

**The stage does NOT contain unmapped historical records unless they were already in canonical tables.** Those records are recoverable from the backup; that is a different guarantee than merging them into live canonical data.

## Step 2 — Database-only activation (NOT recommended until Python is ported)

This is destructive to the **active five-table DB contents** for unmatched legacy records, though the complete old DB is retained. It will make legacy SQL collectors/dashboard fail. Do not activate on a server where you intend to resume the existing Python backend. If you deliberately accept a Java-API-only system until the Python migration is completed, the command is:

```bash
python3 main_back_end/scripts/atlas_offline_five_table.py activate \
  --db /opt/atlas/data/edgar.sqlite3 \
  --stage-file /opt/atlas/tmp/database/edgar.five-tables.TIMESTAMP.stage.sqlite3 \
  --report /opt/atlas/tmp/database/edgar.five-tables.TIMESTAMP.json \
  --acknowledge-archived-history-and-legacy-backend-breakage
```

Replace TIMESTAMP with the **same** timestamp output by stage. Activation checks the original DB has not changed, checks `fuser`, validates full backup and staged hashes, copies stage beside production on the same filesystem, then atomically renames files. It preserves the exact old database as `edgar.sqlite3.before-five-table-TIMESTAMP`. It does not run automatic migrations or restart services.

## Rollback

Stop clients again. Restore the same-filesystem `edgar.sqlite3.before-five-table-TIMESTAMP` original back to `edgar.sqlite3` **only after first renaming the current five-table DB to a separate safe file**. Keep both files and the external archive until you have verified the restoration. Do not restart cron until the old schema is restored or legacy backend queries are replaced.

## What remains to finish the requested end-to-end migration

1. Reconcile the missing legacy source keys, source text versions, old form/company associations and 60 legacy relation IDs into the canonical five tables, with field-level tests using the **populated production data**.
2. Port legacy SQL in 23+ Python production modules including writers, AI scorers and dashboard queries. Preserve backend behavior and tests.
3. Then stage and activate a verified lossless five-table database, and restart Java, Dash, and Atlas cron jobs.

No production database was altered or tested by ChatGPT in this package. Tests were run against a synthetic SQLite database generated from the project schema, not against the populated production database.
