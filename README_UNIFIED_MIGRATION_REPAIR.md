# Atlas unified migration — production-safe trigger rename repair

The earlier migration can fail with:

    error in trigger atlas_mob_comp_ai after rename:
    no such column: company_mobile_summary.company_id

## Why

Older `atlas_mob_*` triggers reference the old `company_mobile_summary` name
inside correlated subqueries. SQLite reparses every trigger during table
renames, and an obsolete reference causes the ALTER TABLE to fail. The earlier
migration tried to rebuild these triggers only **after** renaming.

## Fix

The corrected migration retires only the generated `atlas_mob_*` triggers
**inside the migration transaction, before** renaming the materialized table.
The existing `scripts/migrate_mobile_impact_summary.py` then recreates them
under the new `summary` name in that same transaction. Any unknown trigger/view
still referencing the old name blocks the migration rather than being deleted.

The full SQLite backup is still taken BEFORE the transaction starts. On a
failure, `ROLLBACK` restores the old summary and its triggers.

## Commands

Stop ingestion/scoring/price jobs and `atlas-mobile-api-java` before applying.
Extract this patch over `/opt/atlas/main_back_end`, preserving its relative paths.

    cd /opt/atlas/main_back_end
    python3 scripts/migrate_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --dry-run
    python3 scripts/migrate_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --apply
    python3 scripts/migrate_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --verify
    python3 scripts/audit_legacy_cleanup.py --db /opt/atlas/data/edgar.sqlite3

The last command currently exits with status 2 (BLOCKED), **by design**.
It is read-only and explains why old source tables cannot yet be dropped.
Do not remove the old source tables until their writers and readers have been
fully converted. In this update, `raw_items` and `relationships` are synced
from legacy writers, not yet used as their primary write destination.

## Do not manually delete

`news`, `forms`, `items`, or `news_company_relations`. The installed
`news_pipeline`, `sec_edgar`, `ai` and parts of `dashboard` still refer to them.
Dropping them is not equivalent to removing unused data and will break
production processing. `company_mobile_summary` itself IS renamed to `summary`
by this patch, so it should no longer exist after a successful migration.

No Java backend redeployment is needed to correct the migration script itself.
Use the unified Java API overlay after the database migration verifies.
