#!/usr/bin/env python3
"""SQL regression: canonical Java reads work after legacy physical-table removal."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

root=Path(__file__).resolve().parents[1]
api=(root/'src/main/java/space/aibi/atlas/Api.java').read_text()
dbsource=(root/'src/main/java/space/aibi/atlas/Db.java').read_text()
assert 'CAST(x.source_key AS INTEGER) id' in api
assert 'x.source_key=CAST(? AS TEXT)' in api
assert 'i.legacy_item_id' not in api
assert 'FROM relationships r JOIN raw_items n' in api
assert 'FROM relationships i JOIN raw_items x' in api
assert 'FROM raw_items f JOIN relationships r' in api
assert 'FROM relationships r JOIN raw_items s' in api
assert 'No fallback to news/forms/items' in dbsource

with TemporaryDirectory() as tmp:
    dbpath=Path(tmp)/'canonical-only.sqlite3'
    with sqlite3.connect(dbpath) as db:
        db.executescript('''
          CREATE TABLE companies(id INTEGER PRIMARY KEY,name TEXT,ticker TEXT);
          CREATE TABLE raw_items(id INTEGER PRIMARY KEY,source_type TEXT,source_key TEXT,
            parent_key TEXT,title TEXT,content TEXT,source_url TEXT,
            source_name TEXT,summary TEXT,status TEXT,event_at TEXT,
            received_at TEXT,metadata_json TEXT);
          CREATE TABLE relationships(id INTEGER PRIMARY KEY,company_id INTEGER,
            source_id INTEGER,entity_type TEXT,ai_impact REAL,
            ai_confidence REAL,accumulated_impact REAL,accumulated_monetary REAL);
          CREATE TABLE summary(company_id INTEGER PRIMARY KEY,aggregate_score REAL,
            latest_event_impact REAL);
          INSERT INTO companies VALUES(1,'Example','XYZ');
          INSERT INTO summary VALUES(1,5.0,3.0);
          INSERT INTO raw_items VALUES(1,'news','news-a',NULL,'News','Body',
            'https://example.org/news','Publisher','S','analysed','2026-10-20T00:00:00Z',
            '2026-10-21T00:00:00Z','{}');
          INSERT INTO raw_items VALUES(2,'filing','21',NULL,'Current report','',
            'https://example.org/filing','SEC','','connected','2026-10-21T00:00:00Z',
            '2026-10-21T00:00:00Z','{"form_type":"8-K"}');
          INSERT INTO raw_items VALUES(3,'item','43','https://example.org/filing',
            'Item 2.02','SEC text','https://example.org/item','8-K','','analysed',
            '2026-10-21T00:00:00Z','2026-10-21T00:00:00Z',
            '{"form_id":21,"item_type":"2.02"}');
          INSERT INTO relationships VALUES(1,1,1,'company',2,8,2,0);
          INSERT INTO relationships VALUES(2,1,2,'company',NULL,NULL,NULL,NULL);
          INSERT INTO relationships VALUES(3,1,3,'company',3,7,5,0);
        ''')
        # Exact unified-source predicates used by the Java read layer.
        news=db.execute('''SELECT n.source_key,r.ai_impact,r.accumulated_impact
          FROM relationships r JOIN raw_items n ON n.id=r.source_id AND n.source_type='news'
          WHERE r.company_id=?''',(1,)).fetchone()
        assert news==('news-a',2,2)
        filing=db.execute('''SELECT CAST(f.source_key AS INTEGER),f.title
          FROM raw_items f JOIN relationships r ON r.source_id=f.id
          WHERE f.source_type='filing' AND r.company_id=?''',(1,)).fetchone()
        assert filing==(21,'Current report')
        sec=db.execute('''SELECT CAST(x.source_key AS INTEGER) id,i.ai_impact,
              f.title,i.accumulated_impact FROM relationships i
          JOIN raw_items x ON x.id=i.source_id AND x.source_type='item'
          LEFT JOIN raw_items f ON f.source_type='filing' AND
             (f.source_key=CAST(json_extract(x.metadata_json,'$.form_id') AS TEXT)
              OR (x.parent_key IS NOT NULL AND f.source_url=x.parent_key))
          WHERE i.company_id=? AND x.source_key=CAST(? AS TEXT)''',(1,43)).fetchone()
        assert sec==(43,3,'Current report',5), sec
        history=db.execute('''SELECT s.source_key,r.ai_impact,r.accumulated_impact
          FROM relationships r JOIN raw_items s ON s.id=r.source_id
          WHERE r.company_id=? AND r.ai_impact IS NOT NULL AND s.status!='skipped'
            AND r.ai_impact IS NOT NULL AND s.source_type IN ('news','item')
          ORDER BY julianday(s.event_at) DESC,s.source_type DESC,s.source_key DESC''',(1,)).fetchall()
        assert sorted(row[2] for row in history)==[2,5]
        assert not {'news','forms','items','news_company_relations'} & {
          row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print('PASS: canonical-only News, filing, SEC item and stored accumulated-impact API SQL')
