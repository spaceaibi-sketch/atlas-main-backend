import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('migration', BASE.parent / 'scripts/atlas_five_table_migration.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
INV = json.loads((BASE / 'atlas-db-inventory.json').read_text())


def seeded_db(path):
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        for t in INV['tables']:
            if not t['name'].startswith('sqlite_'):
                db.execute(t['create_sql'])
        db.execute('''INSERT INTO companies(id,cik,name,ticker,exchange,created_at,updated_at)
            VALUES(1,'012345','Test Co','TEST','NYSE','2026-01-01','2026-01-01')''')
        db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,status,created_at,updated_at,article_text)
            VALUES ('n1','Article','https://x','u','t','s','wire','analysed','2026-01-01','2026-01-01','full news content')''')
        db.execute('''INSERT INTO news_company_relations(relation_id,news_id,company_id,entity_key,entity_type,entity_name,status,created_at,updated_at,ai_impact)
            VALUES ('r1','n1',1,'company:1','company','Test Co','analysed','2026-01-01','2026-01-01',5)''')
        db.execute('''INSERT INTO forms(id,company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
            VALUES (5,1,'8-K','Current Report','2026-01-01T00:00:00Z','https://sec.test/5','parsed','2026-01-01','2026-01-01')''')
        db.execute('''INSERT INTO items(id,form_id,item_type,item_name,item_text,status,created_at,updated_at)
            VALUES(7,5,'2.02','Earnings','full filing item text','analysed','2026-01-01','2026-01-01')''')
        db.execute('''INSERT INTO raw_items(id,source_type,source_key,title,content,status,source_name,metadata_json)
            VALUES(11,'news','n1','Article','full news content','analysed','wire','{}')''')
        db.execute('''INSERT INTO raw_items(id,source_type,source_key,title,content,status,source_name,metadata_json)
            VALUES(12,'filing','5','Current Report','','connected','SEC','{}')''')
        db.execute('''INSERT INTO raw_items(id,source_type,source_key,title,content,status,source_name,metadata_json)
            VALUES(13,'item','7','Earnings','full filing item text','analysed','8-K','{}')''')
        db.execute('''INSERT INTO relationships(id,company_id,source_id,entity_key,entity_name,ai_impact,accumulated_impact,
         legacy_relation_id,legacy_item_id,metadata_json)
         VALUES(21,1,11,'company:1','Test Co',5,5,'r1',NULL,'{}')''')
        db.execute('''INSERT INTO relationships(id,company_id,source_id,entity_key,entity_name,ai_impact,accumulated_impact,
         legacy_relation_id,legacy_item_id,metadata_json)
         VALUES(22,1,12,'company:1','Test Co',NULL,NULL,NULL,NULL,'{}')''')
        db.execute('''INSERT INTO relationships(id,company_id,source_id,entity_key,entity_name,ai_impact,accumulated_impact,
         legacy_relation_id,legacy_item_id,metadata_json)
         VALUES(23,1,13,'company:1','Test Co',2,7,NULL,7,'{}')''')
        db.execute('''INSERT INTO summary(company_id,refreshed_at,aggregate_score,last_price)
            VALUES(1,'2026-01-01',7,101.5)''')
        db.execute('''INSERT INTO price_bars(company_id,bar_time_utc,open,high,low,close,volume,source,feed,ingested_at)
            VALUES(1,'2026-01-01T00:00:00Z',100,102,99,101.5,100,'alpaca','sip','2026-01-01')''')
        db.commit()
        # Install every live inventory index/trigger AFTER inserting fixture data.
        # This reproduces real DROP COLUMN trigger/index dependency failures.
        for index in INV['indexes']:
            if index.get('sql'):
                db.execute(index['sql'])
        for trigger in INV['triggers']:
            db.execute(trigger['sql'])
        db.commit()


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / 'live.sqlite3'
        self.dst = Path(self.tmp.name) / 'clean.sqlite3'
        seeded_db(self.src)

    def test_staged_clean_and_price_retention(self):
        before = self.src.stat().st_size
        got = module.build(self.src, self.dst, vacuum=True)
        self.assertEqual(got['price_bars'], 1)
        self.assertTrue(self.src.exists())
        with sqlite3.connect(self.dst) as db:
            self.assertEqual(module.tables(db), set(module.KEEP))
            self.assertEqual(db.execute('SELECT close FROM price_bars').fetchone()[0], 101.5)
            self.assertEqual(db.execute('SELECT count(*) FROM raw_items').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM relationships').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM summary').fetchone()[0], 1)
            self.assertNotIn('legacy_relation_id', module.colnames(db,'relationships'))
            data = {r[0]:json.loads(r[1]) for r in db.execute('SELECT id,metadata_json FROM relationships')}
            self.assertEqual(data[21]['archived_legacy_relation_id'], 'r1')
            self.assertEqual(data[23]['archived_legacy_item_id'], 7)
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertIn('atlas_unified_relationship_update', names)
            self.assertNotIn('atlas_mob_comp_ai', names)
            # Canonical triggers must remain valid after legacy-table removal.
            db.execute('UPDATE relationships SET ai_impact=6 WHERE id=21')
            value = db.execute('SELECT accumulated_impact FROM relationships WHERE id=21').fetchone()[0]
            self.assertIsNotNone(value)
            db.execute("""INSERT INTO price_bars(company_id,bar_time_utc,open,high,low,close,volume,source,feed,ingested_at)
              VALUES(1,'2026-01-02T00:00:00Z',101,103,100,102,110,'alpaca','sip','2026-01-02')""")
            self.assertEqual(db.execute('SELECT last_price FROM summary').fetchone()[0], 102)
        with sqlite3.connect(self.src) as db:
            self.assertIn('news', module.tables(db))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM news').fetchone()[0], 1)

    def test_missing_unified_record_aborts_before_copy(self):
        with sqlite3.connect(self.src) as db:
            db.execute("UPDATE raw_items SET content='WRONG' WHERE source_type='item'")
        with self.assertRaisesRegex(RuntimeError, 'Lossless migration checks'):
            module.build(self.src, self.dst)
        self.assertFalse(self.dst.exists())

    def test_unexpected_table_aborts(self):
        with sqlite3.connect(self.src) as db:
            db.execute('CREATE TABLE another_important_table (id INTEGER PRIMARY KEY)')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected user tables'):
            module.build(self.src, self.dst)
        self.assertFalse(self.dst.exists())

    def test_failed_build_does_not_overwrite_file(self):
        self.dst.write_text('sentinel')
        with self.assertRaisesRegex(RuntimeError, 'Target already exists'):
            module.build(self.src,self.dst)
        self.assertEqual(self.dst.read_text(),'sentinel')


if __name__ == '__main__':
    unittest.main(verbosity=2)
