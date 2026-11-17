"""Integration tests for data-preserving canonical Atlas schema simplification.

Run from main_back_end: python3 tests/test_simplified_unified_model.py
"""
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from sec_edgar.database import connect
from news_pipeline.store import ensure_schema
from scripts.migrate_simplified_unified_model import migrate,verify

OLD_SCHEMA = "\nCREATE TABLE IF NOT EXISTS raw_items (\n id INTEGER PRIMARY KEY,\n source_type TEXT NOT NULL CHECK(source_type IN ('news','filing','item')),\n source_key TEXT NOT NULL,\n parent_source TEXT,\n parent_key TEXT,\n title TEXT NOT NULL DEFAULT '',\n content TEXT NOT NULL DEFAULT '',\n summary TEXT,\n event_at TEXT,\n received_at TEXT,\n source_url TEXT,\n source_name TEXT,\n processing_status TEXT NOT NULL DEFAULT 'created',\n analysis_scope TEXT NOT NULL DEFAULT 'self' CHECK(analysis_scope IN ('self','children')),\n metadata_json TEXT NOT NULL DEFAULT '{}',\n created_at TEXT,\n updated_at TEXT,\n UNIQUE(source_type,source_key)\n);\nCREATE INDEX IF NOT EXISTS idx_raw_type_event ON raw_items(source_type,event_at DESC);\nCREATE INDEX IF NOT EXISTS idx_raw_parent ON raw_items(parent_key,source_type);\nCREATE TABLE IF NOT EXISTS relationships (\n id INTEGER PRIMARY KEY,\n company_id INTEGER REFERENCES companies(id),\n source_id INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,\n entity_type TEXT NOT NULL DEFAULT 'company',\n entity_key TEXT NOT NULL,\n entity_name TEXT NOT NULL DEFAULT '',\n status TEXT NOT NULL DEFAULT 'connected' CHECK(status IN\n  ('connected','queued','analysing','analysed','retry','failed','skipped')),\n correlation_reason TEXT,\n match_evidence TEXT,\n match_hits INTEGER NOT NULL DEFAULT 0,\n ai_impact REAL,\n ai_confidence REAL,\n ai_reason TEXT,\n ai_evidence TEXT,\n ai_monetary_effect REAL,\n ai_monetary_effect_reason TEXT,\n price_change_1w_pct REAL,\n price_change_reason TEXT,\n accumulated_impact REAL,\n accumulated_monetary REAL,\n accumulated_event_count INTEGER,\n accumulated_news_count INTEGER,\n analysed_at TEXT,\n legacy_relation_id TEXT,\n legacy_item_id INTEGER,\n metadata_json TEXT NOT NULL DEFAULT '{}',\n created_at TEXT,\n updated_at TEXT,\n UNIQUE(source_id,entity_key)\n);\nCREATE INDEX IF NOT EXISTS idx_rel_company_status ON relationships(company_id,status);\nCREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);\nCREATE UNIQUE INDEX IF NOT EXISTS idx_rel_legacy_news ON relationships(legacy_relation_id)\n WHERE legacy_relation_id IS NOT NULL;\nCREATE UNIQUE INDEX IF NOT EXISTS idx_rel_legacy_item ON relationships(legacy_item_id)\n WHERE legacy_item_id IS NOT NULL;\n"
STAMP='2026-10-17T12:00:00Z'

def seed(p):
    with connect(p) as db:
        ensure_schema(db)
        db.execute('INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES(?,?,?,?,?)',('0000000001','Alpha','ALPHA',STAMP,STAMP))
        db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,published_utc,status,article_text,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',('n1','Alpha News','https://e/n1','n1','n1','n1','Publisher',STAMP,'analysed','News text',STAMP,STAMP))
        db.execute('''INSERT INTO news_company_relations(relation_id,news_id,company_id,entity_key,entity_type,entity_name,status,ai_impact,ai_confidence,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',('relation1','n1',1,'company:1','company','Alpha','analysed',3,8,STAMP,STAMP))
        db.execute('INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(1,'8-K','Current report',STAMP,'https://sec/f1',STAMP,STAMP))
        db.execute('''INSERT INTO items(form_id,item_type,item_name,item_text,status,ai_impact,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
                   (1,'2.02','Financial results','SEC text','analysed',-1,STAMP,STAMP))
        db.commit()
        db.executescript(OLD_SCHEMA)
        db.execute('CREATE TABLE IF NOT EXISTS summary(company_id INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO summary VALUES(1)')
        db.execute('''INSERT INTO raw_items(source_type,source_key,title,content,source_name,processing_status,analysis_scope,metadata_json)
            VALUES('news','n1','Alpha News','News text','Publisher','extracted','self','{"status":"analysed"}')''')
        db.execute('''INSERT INTO raw_items(source_type,source_key,title,content,parent_source,source_name,processing_status,analysis_scope,metadata_json)
            VALUES('filing','1','Current report','','8-K','SEC','created','children','{"form_type":"8-K","status":"created"}')''')
        db.execute('''INSERT INTO raw_items(source_type,source_key,title,content,parent_source,parent_key,source_name,processing_status,analysis_scope,metadata_json)
            VALUES('item','1','Financial results','SEC text','8-K','https://sec/f1','SEC','parsed','self','{"form_id":1,"status":"analysed"}')''')
        ids={r[0]:r[1] for r in db.execute('SELECT source_type,id FROM raw_items')}
        db.execute('''INSERT INTO relationships(company_id,source_id,entity_type,entity_key,status,ai_impact,accumulated_impact,
            accumulated_event_count,accumulated_news_count,analysed_at,legacy_relation_id,metadata_json)
            VALUES(1,?,'company','company:1','analysed',3,3,1,1,?,'relation1','{"status":"analysed","accumulated_event_count":1}')''',(ids['news'],STAMP))
        db.execute('''INSERT INTO relationships(company_id,source_id,entity_type,entity_key,status,metadata_json)
            VALUES(1,?,'company','company:1','connected','{}')''',(ids['filing'],))
        db.execute('''INSERT INTO relationships(company_id,source_id,entity_type,entity_key,status,ai_impact,
            accumulated_impact,accumulated_event_count,accumulated_news_count,analysed_at,legacy_item_id,metadata_json)
            VALUES(1,?,'company','company:1','analysed',-1,2,2,1,?,1,'{"status":"analysed","accumulated_news_count":1}')''',(ids['item'],STAMP))
        db.commit()

with TemporaryDirectory() as td:
    path=Path(td)/'atlas.sqlite3'
    seed(path)
    with sqlite3.connect(path,isolation_level=None) as db:
        db.execute('PRAGMA foreign_keys=ON')
        before=(db.execute('SELECT COUNT(*) FROM raw_items').fetchone()[0],
                db.execute('SELECT COUNT(*) FROM relationships').fetchone()[0])
        db.execute('BEGIN IMMEDIATE')
        try:
            migrate(db)
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK');raise
        verify(db)
        raw_cols={r[1] for r in db.execute('PRAGMA table_info(raw_items)')}
        rel_cols={r[1] for r in db.execute('PRAGMA table_info(relationships)')}
        assert not raw_cols.intersection(('parent_source','analysis_scope','processing_status'))
        assert not rel_cols.intersection(('status','analysed_at','accumulated_event_count','accumulated_news_count'))
        assert db.execute("SELECT source_name FROM raw_items WHERE source_type='filing'").fetchone()[0]=='SEC'
        assert db.execute("SELECT source_name FROM raw_items WHERE source_type='item'").fetchone()[0]=='8-K'
        assert db.execute("SELECT status FROM raw_items WHERE source_type='news'").fetchone()[0]=='analysed'
        assert db.execute("SELECT json_extract(metadata_json,'$.status') FROM relationships LIMIT 1").fetchone()[0] is None
        assert db.execute("SELECT ai_impact FROM relationships WHERE legacy_item_id=1").fetchone()[0]==-1
        after=(db.execute('SELECT COUNT(*) FROM raw_items').fetchone()[0],
               db.execute('SELECT COUNT(*) FROM relationships').fetchone()[0])
        assert before==after
        db.execute("UPDATE news_company_relations SET ai_impact=4 WHERE relation_id='relation1'")
        assert db.execute("SELECT ai_impact FROM relationships WHERE legacy_relation_id='relation1'").fetchone()[0]==4
        db.execute("UPDATE items SET ai_impact=-2 WHERE id=1")
        assert db.execute("SELECT ai_impact FROM relationships WHERE legacy_item_id=1").fetchone()[0]==-2
        db.execute("UPDATE forms SET form_type='10-Q' WHERE id=1")
        assert db.execute("SELECT source_name FROM raw_items WHERE source_type='item'").fetchone()[0]=='10-Q'
        db.execute("UPDATE news_company_relations SET status='connected' WHERE relation_id='relation1'")
        assert db.execute("SELECT status FROM raw_items WHERE source_type='news'").fetchone()[0]=='connected'
        db.execute("UPDATE news_company_relations SET status='analysed' WHERE relation_id='relation1'")
        assert db.execute("SELECT status FROM raw_items WHERE source_type='news'").fetchone()[0]=='analysed'
        try:db.execute("UPDATE raw_items SET status='failed' WHERE source_type='news'")
        except sqlite3.IntegrityError:pass
        else:raise AssertionError('Invalid status must be rejected')
        verify(db)
        db.execute('BEGIN IMMEDIATE')
        try:
            migrate(db)
            db.execute('COMMIT')
        except BaseException:db.execute('ROLLBACK');raise
        verify(db)
print('PASS: unified schema simplification, data/score retention, four statuses, source names, live triggers, idempotency')
