"""Five-table Atlas data contract; no legacy staging schema or sync triggers."""
from __future__ import annotations

SOURCE_DDL = '''
CREATE TABLE IF NOT EXISTS companies (
 id INTEGER PRIMARY KEY AUTOINCREMENT, cik TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 ticker TEXT, exchange TEXT, acumm_impact REAL NOT NULL DEFAULT 0,
 acumm_monetary REAL NOT NULL DEFAULT 0, accum_confdent REAL NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_items (
 id INTEGER PRIMARY KEY,
 source_type TEXT NOT NULL CHECK(source_type IN ('news','filing','item')),
 source_key TEXT NOT NULL, parent_key TEXT, title TEXT NOT NULL DEFAULT '',
 content TEXT NOT NULL DEFAULT '', summary TEXT, event_at TEXT,received_at TEXT,
 source_url TEXT,source_name TEXT,
 status TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','skipped','connected','analysed')),
 metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT, updated_at TEXT,
 UNIQUE(source_type,source_key)
);
CREATE INDEX IF NOT EXISTS idx_raw_type_event ON raw_items(source_type,event_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_parent ON raw_items(parent_key,source_type);
CREATE TABLE IF NOT EXISTS relationships (
 id INTEGER PRIMARY KEY, company_id INTEGER REFERENCES companies(id),
 source_id INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
 entity_type TEXT NOT NULL DEFAULT 'company', entity_key TEXT NOT NULL,
 entity_name TEXT NOT NULL DEFAULT '', correlation_reason TEXT,match_evidence TEXT,
 match_hits INTEGER NOT NULL DEFAULT 0, ai_impact REAL,ai_confidence REAL,
 ai_reason TEXT,ai_evidence TEXT,ai_monetary_effect REAL,ai_monetary_effect_reason TEXT,
 price_change_1w_pct REAL,price_change_reason TEXT,
 accumulated_impact REAL,accumulated_monetary REAL,
 metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT,updated_at TEXT,
 UNIQUE(source_id,entity_key)
);
CREATE INDEX IF NOT EXISTS idx_rel_company ON relationships(company_id);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
'''
REQUIRED = frozenset({'companies','raw_items','relationships','summary','price_bars'})
FORBIDDEN = frozenset({'forms','items','news','news_company_relations'})


def table_names(db):
    return {r[0] for r in db.execute('SELECT name FROM sqlite_master WHERE type="table" AND name NOT LIKE "sqlite_%"')}


def ensure_five_schema(db):
    """Initialize an empty database or verify an existing five-table database.

    Never silently mutate a populated legacy database or perform historical
    reconciliation: that requires a separately backed-up offline conversion.
    """
    found = table_names(db)
    if found and found != REQUIRED:
        raise RuntimeError(f'Atlas needs five physical tables: found={sorted(found)}; '
                           'offline reconciliation required before running writers')
    if not found:
        from dashboard.mobile_summary import SCHEMA as SUMMARY_SCHEMA
        from price_pipeline.storage import PRICE_SCHEMA
        for ddl in (SOURCE_DDL,SUMMARY_SCHEMA,PRICE_SCHEMA):
            for statement in ddl.split(';'):
                if statement.strip(): db.execute(statement)
        db.commit()
    # Unknown column state is not repaired during regular ingestion.
    if table_names(db) != REQUIRED:
        raise RuntimeError('Failed to initialize canonical five-table database')
    db.execute('PRAGMA foreign_keys=ON')


def metadata(value):
    import json
    return json.dumps(value, ensure_ascii=False, separators=(',',':'),sort_keys=True)
