#!/usr/bin/env python3
"""Backfill per-News/per-SEC cumulative impact in the writer-owned Atlas SQLite DB.

Definition: retrospective sum of currently analyzed, non-null signed ai_impact
values for a company, ordered by the ORIGINAL News published_utc or SEC filed_date
(UTC). This is not the seven-day age/confidence weighted `companies.acumm_impact`.

A persistent company_event_impact_history table stores the history. Trigger-based
per-company recalculation maintains it across later inserts, updates, and deletes.
Java's read-only mobile API only SELECTs these materialized results.

Usage:
  python3 scripts/migrate-accumulated-impact-history.py --db /path/to/atlas.sqlite3 --dry-run
  python3 scripts/migrate-accumulated-impact-history.py --db /path/to/atlas.sqlite3 --apply
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

TABLE = 'company_event_impact_history'
COLUMNS = ('company_id', 'kind', 'record_key', 'record_id', 'event_at', 'impact',
           'accumulated_impact', 'accumulated_event_count', 'accumulated_news_count')
REQUIRED = {
    'companies': {'id'},
    'news': {'news_id', 'published_utc'},
    'news_company_relations': {'relation_id', 'news_id', 'company_id', 'status', 'ai_impact'},
    'forms': {'id', 'company_id', 'filed_date'},
    'items': {'id', 'form_id', 'status', 'ai_impact'},
}


def validate(db: sqlite3.Connection) -> None:
    for table, required in REQUIRED.items():
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if not required <= columns:
            missing = ', '.join(sorted(required - columns))
            raise RuntimeError(f'Missing required table/columns: {table}: {missing}')
    if db.execute('PRAGMA user_version').fetchone() is None:
        raise RuntimeError('Cannot read Atlas database schema')


def event_select(target: str) -> str:
    """SQL for one/many company IDs, filtering BEFORE analytic windows.

    record_key is relation_id for News (a news_id can have several company
    associations) and items.id for SEC; record_id is the public API ID.
    Invalid/missing source timestamps are retained, sorted FIRST, so the
    final historical total agrees with the current aggregate_score. Such
    events cannot be plotted on a time X-axis until their dates are repaired.
    """
    source = f'''SELECT r.company_id company_id,'news' kind,CAST(r.relation_id AS TEXT) record_key,
                    CAST(n.news_id AS TEXT) record_id,n.published_utc event_at,
                    CAST(r.ai_impact AS REAL) impact
               FROM news_company_relations r JOIN news n ON n.news_id=r.news_id
               WHERE r.company_id IN ({target}) AND r.status='analysed' AND r.ai_impact IS NOT NULL
               UNION ALL
               SELECT f.company_id,'item',CAST(i.id AS TEXT),CAST(i.id AS TEXT),f.filed_date,
                      CAST(i.ai_impact AS REAL)
               FROM items i JOIN forms f ON f.id=i.form_id
               WHERE f.company_id IN ({target}) AND i.status='analysed' AND i.ai_impact IS NOT NULL'''
    order = "CASE WHEN julianday(event_at) IS NULL THEN 0 ELSE 1 END,julianday(event_at),kind,record_key"
    return f'''SELECT company_id,kind,record_key,record_id,event_at,impact,
                      SUM(impact) OVER (PARTITION BY company_id ORDER BY {order}
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) accumulated_impact,
                      COUNT(*) OVER (PARTITION BY company_id ORDER BY {order}
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) accumulated_event_count,
                      SUM(CASE WHEN kind='news' THEN 1 ELSE 0 END)
                       OVER (PARTITION BY company_id ORDER BY {order}
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) accumulated_news_count
               FROM ({source})'''


def rebuild_sql(target: str) -> str:
    # Trigger bodies accept standard DELETE/INSERT statements; window functions
    # run inside INSERT's SELECT, not a top-level WITH (SQLite trigger restriction).
    return (f'DELETE FROM {TABLE} WHERE company_id IN ({target});\n'
            f'INSERT INTO {TABLE} ({", ".join(COLUMNS)}) {event_select(target)};')


def trigger_specs() -> list[tuple[str, str, str]]:
    # Changes to source attribution, dates, analysis score, or parent company
    # rebuild BOTH affected companies when a record moves between them.
    return [
        ('relations_ai', 'AFTER INSERT ON news_company_relations', 'SELECT NEW.company_id'),
        ('relations_ad', 'AFTER DELETE ON news_company_relations', 'SELECT OLD.company_id'),
        ('relations_au', 'AFTER UPDATE OF company_id,news_id,relation_id,status,ai_impact ON news_company_relations',
         'SELECT OLD.company_id UNION SELECT NEW.company_id'),
        ('news_ai', 'AFTER INSERT ON news',
         'SELECT DISTINCT company_id FROM news_company_relations WHERE news_id=NEW.news_id'),
        ('news_ad', 'AFTER DELETE ON news',
         'SELECT DISTINCT company_id FROM news_company_relations WHERE news_id=OLD.news_id'),
        ('news_au', 'AFTER UPDATE OF news_id,published_utc ON news',
         'SELECT DISTINCT company_id FROM news_company_relations WHERE news_id=OLD.news_id OR news_id=NEW.news_id'),
        ('items_ai', 'AFTER INSERT ON items',
         'SELECT company_id FROM forms WHERE id=NEW.form_id'),
        ('items_ad', 'AFTER DELETE ON items',
         'SELECT company_id FROM forms WHERE id=OLD.form_id'),
        ('items_au', 'AFTER UPDATE OF id,form_id,status,ai_impact ON items',
         'SELECT company_id FROM forms WHERE id=OLD.form_id UNION SELECT company_id FROM forms WHERE id=NEW.form_id'),
        ('forms_ai', 'AFTER INSERT ON forms', 'SELECT NEW.company_id'),
        ('forms_ad', 'AFTER DELETE ON forms', 'SELECT OLD.company_id'),
        ('forms_au', 'AFTER UPDATE OF id,company_id,filed_date ON forms',
         'SELECT OLD.company_id UNION SELECT NEW.company_id'),
    ]


def migrate(db: sqlite3.Connection) -> None:
    validate(db)
    db.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('news','item')),
        record_key TEXT NOT NULL,
        record_id TEXT NOT NULL,
        event_at TEXT,
        impact REAL NOT NULL,
        accumulated_impact REAL NOT NULL,
        accumulated_event_count INTEGER NOT NULL,
        accumulated_news_count INTEGER NOT NULL,
        PRIMARY KEY(company_id,kind,record_key)
    )''')
    cols = {row[1] for row in db.execute(f'PRAGMA table_info("{TABLE}")')}
    if not set(COLUMNS) <= cols:
        raise RuntimeError(f'Historical impact table {TABLE} has incompatible columns')
    db.execute(f'CREATE INDEX IF NOT EXISTS idx_atlas_hist_date ON {TABLE}(company_id,event_at,kind,record_key)')
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_hist_%'").fetchall():
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute(f'DELETE FROM {TABLE}')
    db.execute(f'INSERT INTO {TABLE} ({", ".join(COLUMNS)}) '+event_select('SELECT id FROM companies'))
    for name, event, target in trigger_specs():
        db.execute(f'CREATE TRIGGER atlas_hist_{name} {event} BEGIN\n{rebuild_sql(target)}\nEND')
    # Foreign-key checks for our materialized rows (when callers opt into FK enforcement).
    faults = db.execute(f'PRAGMA foreign_key_check({TABLE})').fetchall()
    if faults:
        raise RuntimeError(f'Foreign-key integrity check failed: {faults[:5]}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='WRITER-OWNED SQLite database path (SEC_DB_PATH)')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--apply', action='store_true')
    options = parser.parse_args()
    path = options.db.expanduser().resolve(strict=True)
    if not path.is_file():
        raise RuntimeError('Not a regular SQLite database file: '+str(path))
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        validate(db)
        total = db.execute('''SELECT
           (SELECT COUNT(*) FROM news_company_relations WHERE status='analysed' AND ai_impact IS NOT NULL) +
           (SELECT COUNT(*) FROM items WHERE status='analysed' AND ai_impact IS NOT NULL)''').fetchone()[0]
        print('Atlas schema OK; approximately', total, 'assessed News + SEC rows to inspect')
        print('Source dates: news.published_utc and forms.filed_date (UTC)')
    if options.dry_run:
        print('DRY RUN: database unchanged')
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = path.with_name(path.name+'.pre-impact-history-'+stamp+'.bak')
    if backup.exists():
        raise RuntimeError('Refusing to overwrite existing backup: '+str(backup))
    with sqlite3.connect(path, timeout=60) as original, sqlite3.connect(backup) as saved:
        original.execute('PRAGMA busy_timeout=60000')
        original.backup(saved)
        if saved.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Backup integrity_check failed; no migration performed')
    print('SQLite backup:', backup)
    with sqlite3.connect(path, timeout=60, isolation_level=None) as db:
        db.execute('PRAGMA busy_timeout=60000')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            migrate(db)
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
        print('Stored historical events:', db.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0])
        print('Installed update triggers:', db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_hist_%'").fetchone()[0])
        print('Migration complete; safe to restart the Java API.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print('Migration FAILED:', exc, file=sys.stderr)
        sys.exit(1)
