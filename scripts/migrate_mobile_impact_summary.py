#!/usr/bin/env python3
"""Migrate main_back_end's WRITER-OWNED SQLite database to transactionally maintained mobile summaries.

Usage:
  python3 scripts/migrate_mobile_impact_summary.py --db /path/to/atlas.sqlite3 --dry-run
  python3 scripts/migrate_mobile_impact_summary.py --db /path/to/atlas.sqlite3 --apply

Stop bulk ingestion for the migration, retain the auto-backup; no API restart alone
can populate columns until this migration and updated Java code are deployed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

FIELDS = {
    "max_news_impact": "REAL",
    "max_sec_impact": "REAL",
    "latest_event_impact": "REAL",
    "latest_event_kind": "TEXT",
    "latest_event_at": "TEXT",
    "max_news_at": "TEXT",
    "max_sec_at": "TEXT",
    "last_assessed_at": "TEXT",
    "latest_confidence": "REAL",
}
NEEDED = {
    "companies": {"id"},
    "summary": {"company_id", "refreshed_at", "aggregate_score", "latest_published", "last_news_received_at", "latest_filing", "latest_item_filed", "related_news_count", "scored_news_count", "scored_items_count", "sec_items_count", "num_forms", "last_price", "last_price_bar_at", "trend_24h", "trend_7d"},
    "forms": {"id", "company_id", "filed_date", "created_at"},
    "items": {"id", "form_id", "status", "ai_impact", "ai_confidence", "updated_at"},
    "news": {"news_id", "status", "published_utc", "created_at"},
    "news_company_relations": {"relation_id", "news_id", "company_id", "status", "ai_impact", "ai_confidence", "updated_at"},
}


def validate(db: sqlite3.Connection) -> bool:
    for name, required in NEEDED.items():
        have = {r[1] for r in db.execute(f'PRAGMA table_info("{name}")')}
        missing = required - have
        if missing:
            raise RuntimeError(f'{name}: missing required columns: {", ".join(sorted(missing))}; wrong database or schema version')
    price_cols = {r[1] for r in db.execute('PRAGMA table_info("price_bars")')}
    if price_cols and not {"company_id", "bar_time_utc", "close"} <= price_cols:
        raise RuntimeError('price_bars exists but lacks company_id/bar_time_utc/close; cannot ensure price summary freshness')
    return bool(price_cols)


# Set of expressions recalculated from raw records for EACH affected company.
# UTC timestamps are normalized so ISO8601 variants sort consistently.
_CTES = """
WITH
 n AS MATERIALIZED (
   SELECT r.news_id,r.relation_id,r.status, r.ai_impact impact,r.ai_confidence confidence,
     r.updated_at assessed_at, z.status news_status,z.published_utc,z.created_at,
     COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',NULLIF(TRIM(z.published_utc),'')),
              strftime('%Y-%m-%dT%H:%M:%fZ',NULLIF(TRIM(z.created_at),''))) event_at
   FROM news_company_relations r JOIN news z ON z.news_id=r.news_id
   WHERE r.company_id=summary.company_id
 ),
 f AS MATERIALIZED (SELECT id,filed_date,created_at,
       COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',NULLIF(TRIM(filed_date),'')),
                strftime('%Y-%m-%dT%H:%M:%fZ',NULLIF(TRIM(created_at),''))) event_at
       FROM forms WHERE company_id=summary.company_id),
 i AS MATERIALIZED (
   SELECT a.id,a.status,a.ai_impact impact,a.ai_confidence confidence,a.updated_at assessed_at,
          f.filed_date,f.event_at
   FROM items a JOIN f ON f.id=a.form_id
 ),
 scored AS MATERIALIZED (
   SELECT 'news' kind,impact,confidence,assessed_at,event_at,relation_id record_id
   FROM n WHERE status='analysed' AND impact IS NOT NULL
   UNION ALL
   SELECT 'sec',impact,confidence,assessed_at,event_at,CAST(id AS TEXT)
   FROM i WHERE status='analysed' AND impact IS NOT NULL
 ),
 mxn AS (SELECT impact,event_at FROM scored WHERE kind='news'
         ORDER BY ABS(impact) DESC,(event_at IS NULL),event_at DESC,record_id DESC LIMIT 1),
 mxs AS (SELECT impact,event_at FROM scored WHERE kind='sec'
         ORDER BY ABS(impact) DESC,(event_at IS NULL),event_at DESC,record_id DESC LIMIT 1),
 latest AS (SELECT impact,kind,event_at FROM scored WHERE event_at IS NOT NULL
            ORDER BY event_at DESC,kind ASC,record_id DESC LIMIT 1),
 last_assessment AS (SELECT confidence FROM scored
                   ORDER BY assessed_at DESC,kind ASC,record_id DESC LIMIT 1)
"""

_FULL = [
    "(SELECT COUNT(DISTINCT news_id) FROM n WHERE news_status NOT IN ('created','skipped'))",  # related_news_count
    "(SELECT COUNT(*) FROM n WHERE status='analysed' AND impact IS NOT NULL)",  # scored_news_count
    "(SELECT COUNT(*) FROM i WHERE status='analysed' AND impact IS NOT NULL)",  # scored_items_count
    "(SELECT COUNT(*) FROM i)",  # sec_items_count
    "(SELECT COUNT(*) FROM f)",  # num_forms
    "(SELECT MAX(published_utc) FROM n WHERE news_status NOT IN ('created','skipped'))", # latest_published
    "(SELECT MAX(created_at) FROM n WHERE news_status NOT IN ('created','skipped'))", # last_news_received_at
    "(SELECT MAX(filed_date) FROM f)", # latest_filing
    "(SELECT MAX(filed_date) FROM i)", # latest_item_filed
    "(SELECT SUM(impact) FROM scored)", # aggregate_score
]
_METRICS = [
    "(SELECT impact FROM mxn)",
    "(SELECT impact FROM mxs)",
    "(SELECT impact FROM latest)",
    "(SELECT kind FROM latest)",
    "(SELECT event_at FROM latest)",
    "(SELECT event_at FROM mxn)",
    "(SELECT event_at FROM mxs)",
    "(SELECT MAX(assessed_at) FROM scored)",
    "(SELECT confidence FROM last_assessment)",
]
_COLS_FULL = ["related_news_count","scored_news_count","scored_items_count", "sec_items_count",
              "num_forms","latest_published","last_news_received_at","latest_filing",
              "latest_item_filed","aggregate_score"]


def refresh(target: str, *, full: bool = True, has_news: bool = True) -> str:
    cols = (_COLS_FULL if full else []) + list(FIELDS)
    values = (_FULL if full else []) + _METRICS
    # Every assessment update changes the materialized values and their freshness
    # marker, including lightweight metric-only repairs after legacy writes.
    cols += ["refreshed_at"]
    values += ["strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
    ctes = _CTES
    if not has_news:
        # Fresh SEC-only writer databases may not have initialized news tables.
        # Keep the SEC-derived Impact fields available without synthetic news.
        start = ctes.index(' n AS MATERIALIZED (')
        end = ctes.index(' f AS MATERIALIZED (', start)
        ctes = (ctes[:start] + " n AS MATERIALIZED (SELECT NULL news_id,NULL relation_id,"
                "NULL status,NULL impact,NULL confidence,NULL assessed_at,"
                "NULL news_status,NULL published_utc,NULL created_at,NULL event_at WHERE 0),\n "
                + ctes[end:])
    return ("UPDATE summary SET ("+','.join(cols)+") = ("+
            ctes+"SELECT "+",\n".join(values)+") WHERE company_id IN ("+target+");")


def ensure(target: str) -> str:
    return ("INSERT OR IGNORE INTO summary(company_id,refreshed_at) "
            "SELECT id,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM companies WHERE id IN ("+target+");")


def refresh_trigger(name: str, when: str, table: str, target: str, *, create: bool = True, only_metrics: bool = False) -> str:
    return (f'CREATE TRIGGER "{name}" {when} ON "{table}" BEGIN\n'+
           (ensure(target)+'\n' if create else '')+
           refresh(target,full=not only_metrics)+'\nEND;')


def price_refresh(target: str) -> str:
    # Baseline = most recent actual observation at or before the lookback from
    # newest stored bar. No imputation; NULL when history is insufficient.
    return """UPDATE summary SET
    refreshed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    (last_price,last_price_bar_at,trend_24h,trend_7d) = (
      WITH cur AS (SELECT close,bar_time_utc FROM price_bars p
                  WHERE p.company_id=summary.company_id AND p.close IS NOT NULL
                  ORDER BY p.bar_time_utc DESC LIMIT 1),
           base24 AS (SELECT close FROM price_bars p WHERE p.company_id=summary.company_id
                    AND p.close IS NOT NULL AND julianday(p.bar_time_utc)<=julianday((SELECT bar_time_utc FROM cur))-1
                    ORDER BY p.bar_time_utc DESC LIMIT 1),
           base7 AS (SELECT close FROM price_bars p WHERE p.company_id=summary.company_id
                    AND p.close IS NOT NULL AND julianday(p.bar_time_utc)<=julianday((SELECT bar_time_utc FROM cur))-7
                    ORDER BY p.bar_time_utc DESC LIMIT 1)
      SELECT (SELECT close FROM cur),(SELECT bar_time_utc FROM cur),
             CASE WHEN (SELECT close FROM base24) IS NOT NULL AND (SELECT close FROM base24)!=0
                  THEN 100.0*((SELECT close FROM cur)/(SELECT close FROM base24)-1.0) END,
             CASE WHEN (SELECT close FROM base7) IS NOT NULL AND (SELECT close FROM base7)!=0
                  THEN 100.0*((SELECT close FROM cur)/(SELECT close FROM base7)-1.0) END
    ) WHERE company_id IN ("""+target+");"


def price_trigger(name: str, when: str, target: str) -> str:
    return f'CREATE TRIGGER "{name}" {when} ON price_bars BEGIN\n'+ensure(target)+'\n'+price_refresh(target)+'\nEND;'


def generate_triggers(price_present: bool) -> list[str]:
    specs = [
        ('atlas_mob_comp_ai','AFTER INSERT','companies','NEW.id'),
        ('atlas_mob_items_ai','AFTER INSERT','items','(SELECT company_id FROM forms WHERE id=NEW.form_id)'),
        ('atlas_mob_items_ad','AFTER DELETE','items','(SELECT company_id FROM forms WHERE id=OLD.form_id)'),
        ('atlas_mob_items_au','AFTER UPDATE OF form_id,status,ai_impact,ai_confidence,updated_at','items',
         '(SELECT company_id FROM forms WHERE id=OLD.form_id) UNION SELECT company_id FROM forms WHERE id=NEW.form_id'),
        ('atlas_mob_forms_ai','AFTER INSERT','forms','NEW.company_id'),
        ('atlas_mob_forms_ad','AFTER DELETE','forms','OLD.company_id'),
        ('atlas_mob_forms_au','AFTER UPDATE OF company_id,filed_date,created_at','forms',
         'OLD.company_id UNION SELECT NEW.company_id'),
        ('atlas_mob_news_rel_ai','AFTER INSERT','news_company_relations','NEW.company_id'),
        ('atlas_mob_news_rel_ad','AFTER DELETE','news_company_relations','OLD.company_id'),
        ('atlas_mob_news_rel_au','AFTER UPDATE OF company_id,news_id,status,ai_impact,ai_confidence,updated_at','news_company_relations',
         'OLD.company_id UNION SELECT NEW.company_id'),
        ('atlas_mob_news_ai','AFTER INSERT','news',
         'SELECT DISTINCT company_id FROM news_company_relations WHERE news_id=NEW.news_id'),
        ('atlas_mob_news_au','AFTER UPDATE OF published_utc,created_at,status','news',
         'SELECT DISTINCT company_id FROM news_company_relations WHERE news_id=NEW.news_id'),
        ('atlas_mob_news_ad','AFTER DELETE','news',
         'SELECT DISTINCT company_id FROM news_company_relations WHERE news_id=OLD.news_id'),
    ]
    # IDs are formatted as a query, not as expressions, to allow multi-company updates.
    out=[]
    for name,when,table,query in specs:
        target= query if query.startswith('SELECT ') else ('SELECT '+query)
        out.append(refresh_trigger(name,when,table,target))
    out.append('CREATE TRIGGER atlas_mob_comp_ad AFTER DELETE ON companies BEGIN DELETE FROM summary WHERE company_id=OLD.id; END;')
    # Protect materialized impact fields when legacy summary writer performs
    # INSERT OR REPLACE, or UPDATE of its existing summary values.
    # Legacy writers sometimes INSERT OR REPLACE old summary columns, wiping the new
    # metrics and even cached price fields. Rebuild the whole inserted row from raw
    # events, and (when available) the actual price bars in the same transaction.
    summary_insert = refresh_trigger('atlas_mob_summary_ai','AFTER INSERT',
                     'summary','SELECT NEW.company_id',create=False)
    if price_present:
        summary_insert=summary_insert.replace('\nEND;', '\n'+price_refresh('SELECT NEW.company_id')+'\nEND;')
    out.append(summary_insert)
    out.append(refresh_trigger('atlas_mob_summary_au',
            'AFTER UPDATE OF aggregate_score,related_news_count,scored_news_count,scored_items_count,num_forms,latest_published,latest_filing',
            'summary','SELECT NEW.company_id',create=False,only_metrics=True))
    if price_present:
        for name,when,target in [
            ('atlas_mob_price_ai','AFTER INSERT','SELECT NEW.company_id'),
            ('atlas_mob_price_ad','AFTER DELETE','SELECT OLD.company_id'),
            ('atlas_mob_price_au','AFTER UPDATE OF company_id,bar_time_utc,close',
             'SELECT OLD.company_id UNION SELECT NEW.company_id'),
        ]:
            out.append(price_trigger(name,when,target))
    return out


def migrate(db: sqlite3.Connection, price_present: bool) -> None:
    cols = {r[1] for r in db.execute('PRAGMA table_info("summary")')}
    for name, typ in FIELDS.items():
        if name not in cols:
            db.execute(f'ALTER TABLE summary ADD COLUMN {name} {typ}')
    indexes = [
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_ncr_c ON news_company_relations(company_id,news_id)',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_ncr_n ON news_company_relations(news_id,company_id)',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_ncr_scored ON news_company_relations(company_id,ABS(ai_impact),news_id) WHERE status=\'analysed\' AND ai_impact IS NOT NULL',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_items_f ON items(form_id)',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_items_scored ON items(form_id,ABS(ai_impact),id) WHERE status=\'analysed\' AND ai_impact IS NOT NULL',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_forms_c ON forms(company_id,filed_date,id)',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_score ON summary(ROUND(aggregate_score,2) DESC,company_id)',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_recent_news ON summary(latest_published DESC,company_id)',
       'CREATE INDEX IF NOT EXISTS idx_atlas_mob_recent_sec ON summary(latest_item_filed DESC,company_id)',
    ]
    for metric in ('max_news_impact','max_sec_impact','latest_event_impact'):
        indexes.append(f'CREATE INDEX IF NOT EXISTS idx_atlas_mob_{metric} ON summary(ROUND({metric},2) DESC, company_id) WHERE {metric} IS NOT NULL')
    if price_present:
        indexes.append('CREATE INDEX IF NOT EXISTS idx_atlas_mob_price_at ON price_bars(company_id,bar_time_utc DESC)')
    for sql in indexes:
        db.execute(sql)
    # Disable old atlas_mob_ triggers during the one-time backfill: otherwise
    # every inserted/updated summary row redundantly recalculates itself.
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_mob_%'").fetchall():
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute("INSERT OR IGNORE INTO summary(company_id,refreshed_at) SELECT id,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM companies")
    db.execute(refresh('SELECT id FROM companies'))
    if price_present:
        db.execute(price_refresh('SELECT id FROM companies'))
    # Install the triggers only AFTER the backfill, in the SAME atomic migration.
    for statement in generate_triggers(price_present):
        db.execute(statement)


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True,help='WRITER-OWNED Atlas SQLite DB (not the Java JAR or a copied directory)')
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--apply',action='store_true',help='back up and perform transactionally')
    group.add_argument('--dry-run',action='store_true',help='read-only schema validation')
    a=p.parse_args()
    path=a.db.expanduser().resolve(strict=True)
    if not path.is_file():raise RuntimeError(f'Not a regular file: {path}')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        price_present=validate(db)
        print('Schema OK; price bars:', 'present, triggers included' if price_present else 'absent (price fields remain writer-managed)')
        print('Summary columns to ensure:', ', '.join(FIELDS))
        print('Expected update triggers:', len(generate_triggers(price_present)))
    if a.dry_run:return 0
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=path.with_name(path.name+'.pre-mobile-impact-'+stamp+'.bak')
    if backup.exists():raise RuntimeError(f'Backup already exists: {backup}')
    with sqlite3.connect(path) as src,sqlite3.connect(backup) as dest:
        src.backup(dest)  # SQLite online backup safely includes committed WAL data.
        result = dest.execute('PRAGMA integrity_check').fetchone()
        if result is None or result[0] != 'ok':
            raise RuntimeError('SQLite migration backup failed integrity_check; migration not applied')
    print('SQLite backup:',backup)
    with sqlite3.connect(path,timeout=45,isolation_level=None) as db:
        db.execute('PRAGMA busy_timeout=45000')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            validate(db)
            migrate(db,price_present)
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
        n=db.execute('SELECT COUNT(*) FROM summary').fetchone()[0]
        cols=[r[1] for r in db.execute('PRAGMA table_info(summary)')]
        print(f'Migration complete. {n} company summaries; new columns verified: {all(x in cols for x in FIELDS)}')
        print('WAL/foreign key behavior of the WRITER still depends on its own connection settings.')
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,sqlite3.Error,RuntimeError) as exc:
        print('Migration FAILED:',exc,file=sys.stderr)
        sys.exit(1)
