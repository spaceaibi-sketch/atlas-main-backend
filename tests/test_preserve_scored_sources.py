"""Regression: 24-hour expiration cannot hide already-saved AI assessments."""
from datetime import datetime, timezone
import sqlite3
from shared.stale_created import skip_created


def test_stale_created_only_skips_unscored_records():
    conn=sqlite3.connect(':memory:')
    try:
        conn.executescript('''
          CREATE TABLE raw_items(id INTEGER PRIMARY KEY,source_type TEXT,source_key TEXT,
            status TEXT,metadata_json TEXT,created_at TEXT,updated_at TEXT);
          CREATE TABLE relationships(source_id INTEGER,ai_impact REAL);
          INSERT INTO raw_items VALUES
            (1,'news','news-scored','created','{}','2026-11-01T00:00:00Z',NULL),
            (2,'item','sec-scored','created','{}','2026-11-01T00:00:00Z',NULL),
            (3,'item','sec-pending','created','{}','2026-11-01T00:00:00Z',NULL);
          INSERT INTO relationships VALUES (1,0),(2,-3),(3,NULL);
        ''')
        changes=skip_created(conn,now=datetime(2026,11,5,tzinfo=timezone.utc))
        assert changes=={'news':0,'filing':0,'item':1}
        state=dict(conn.execute('SELECT source_key,status FROM raw_items'))
        assert state=={'news-scored':'created','sec-scored':'created','sec-pending':'skipped'}
    finally:
        conn.close()
