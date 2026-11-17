# Main backend: mobile impact summary transition

This overlay patches **main_back_end**, the SQLite WRITER of record. It is not a
Java API overlay. It complements (does not replace) the previously delivered
`android_back_and_java` Java API update, which reads these saved columns.

## What stays materialized

`company_mobile_summary` has these additional SQLite columns:

- `max_news_impact`, `max_sec_impact`: signed score of the assessed event
  with maximum absolute impact, with deterministic recency tie-breaking.
- `latest_event_impact`: signed assessed impact of the newest news/SEC event.
- `latest_event_kind`, `latest_event_at`: source (`news`/`sec`) and event UTC time.
- `max_news_at`, `max_sec_at`: date/time of the chosen max-impact record.
- `last_assessed_at`, `latest_confidence`: latest scoring metadata.

The existing aggregated, price, and 24h/7d fields are unchanged. NULL means
not assessed/insufficient data (and an assessed zero stays zero).

## Upgrade existing database (one time)

1. Stop/pause ALL writing jobs (SEC, news, scoring, price collection, cron) and
   stop the Java API while applying the transition, to avoid concurrent writes.
2. Overlay the changed files on the corresponding `main_back_end/` paths.
3. From the **main_back_end project directory** run:

```bash
python3 scripts/migrate_mobile_impact_summary.py --db /ABSOLUTE/PATH/TO/WRITABLE/atlas.sqlite3 --dry-run
python3 scripts/migrate_mobile_impact_summary.py --db /ABSOLUTE/PATH/TO/WRITABLE/atlas.sqlite3 --apply
```

`--apply` takes an SQLite online backup named
`<db>.pre-mobile-impact-<UTC timestamp>.bak`, validates required tables, adds
missing columns/indexes, backfills ALL existing companies, and installs
transactional triggers. It can be re-run safely; a new backup is taken every
run. The script will refuse an incompatible schema rather than guess.

4. Deploy/restart the `main_back_end` writer processes with this source, then
   restart the Java API **using the earlier materialized-API JAR/source update**.
   Restarting Java alone does not change the writer or database columns.

## Automatic updates

- News relation analysis/edits/deletions: re-materialize that company's
  assessment metrics in the same database transaction as the source write.
- SEC item analysis/edits/deletions, including form reassignment: refresh
  affected companies, including old and new owners.
- Price bar insert/update/delete: update last price/24h/7d *only*, without
  scanning news/SEC assessments. Baselines require actual stored candles.
- Legacy company summary insert/update: restore the new assessment fields.
- AI scoring job post-processing: when write triggers exist, skip the redundant
  full-company mobile refresh; the other backend caches are not skipped.

The updated `price_pipeline/storage.py` keeps its inserted-price-bar count
accurate even when triggers write summary rows and safely preserves known
mobile-summary triggers during the optional legacy yfinance feed upgrade.

## Verify

```bash
python3 - <<'PY'
import sqlite3
p='/ABSOLUTE/PATH/TO/WRITABLE/atlas.sqlite3'
with sqlite3.connect(p) as db:
    print(list(db.execute("SELECT name FROM pragma_table_info('company_mobile_summary') WHERE name IN ('max_news_impact','max_sec_impact','latest_event_impact')")))
    print(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_mob_%'").fetchone())
    print(list(db.execute("SELECT company_id,aggregate_score,max_news_impact,max_sec_impact,latest_event_impact,last_price,trend_24h,trend_7d FROM company_mobile_summary LIMIT 5")))
PY
```

## Operational notes

- The trigger migration requires the `companies`, `forms`, `items`, `news`,
  `news_company_relations`, and `company_mobile_summary` tables from the main
  backend. An optional compatible `price_bars` table is automatically detected.
- Install the migration against the writer's real database, not a Java-side
  replica or SQLite file attached elsewhere.
- SQLite triggers installed by the migration continue working across
  process restarts and apply to direct SQL changes as well as Python writers.
- Deploy new Java/API sources from the previous API update separately; this
  ZIP contains only main backend changed files.
- Validate on a staging copy before applying to production, and preserve the
  timestamped pre-migration backup.
