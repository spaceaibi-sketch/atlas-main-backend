"""v10 surrogate-key migration, Item collisions and write-once response archives."""
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from sec_edgar.database import connect,add_form,store_items
from sec_edgar.form_parser import ParsedItem
from sec_edgar.forms_cli import process_forms
from sec_edgar.parsing import Filing
from sec_edgar.raw_archive import filing_subject, accession_from_url

ACC='0001193125-26-424052'
INDEX=f'https://www.sec.gov/Archives/edgar/data/900075/{ACC.replace("-", "")}/{ACC}-index.htm'
PRIMARY=INDEX.rsplit('/',1)[0]+'/d177727d8k.htm'
DOC=(b'<html><body><h1>8-K</h1><h2>Item 8.01 Other Events</h2>'
     b'<p>The company disclosed a new agreement.</p><h2>Item 9.01 Exhibits</h2></body></html>')
HTML=(f'<table class="tableFile"><tr><td>1</td><td>8-K</td><td>'
      f'<a href="/ix?doc={PRIMARY.split("www.sec.gov")[1]}">report</a></td>'
      f'<td>8-K</td><td>18</td></tr></table>').encode()

def filing():
    return Filing(ACC,'8-K','0000900075','Example Issuer','2026-10-16',
                  '2026-10-16T20:00:00Z','atom_updated',INDEX,'',('8.01',),'atom')

def test_fresh_schema_keys_and_distinct_item_parts():
    with TemporaryDirectory() as temp:
        with connect(Path(temp)/'edgar.sqlite3') as c:
            assert add_form(c,filing())=='inserted'
            assert add_form(c,filing())=='already_seen'
            f=c.execute('SELECT id,filed_date FROM forms').fetchone()
            assert f['filed_date']=='2026-10-16T20:00:00Z'
            for t,col in [('companies','source'),('forms','accession'),('forms','filing_ts_utc'),('forms','time_source'),('forms','summary'),('items','part'),('items','item_guid')]:
                assert col not in {r[1] for r in c.execute(f'PRAGMA table_info({t})')}
            parts=[ParsedItem('1','PART I','FIRST SECTION','first text'),
                   ParsedItem('1','PART II','SECOND SECTION','second text')]
            # Other form types allow all SEC Items, unlike the 8-K watched Item filter.
            c.execute("UPDATE forms SET form_type='10-Q' WHERE id=?",(f['id'],))
            store_items(c,f['id'],'123',parts)
            rows=c.execute('SELECT item_occurrence,item_name FROM items ORDER BY item_occurrence').fetchall()
            assert [r['item_occurrence'] for r in rows]==[1,2]
            assert 'PART II' in rows[1]['item_name']
            assert c.execute('PRAGMA foreign_key_check').fetchall()==[]

def test_filing_archive_index_ix_wrapper_and_primary():
    class Client:
        request_count=0
        def get(self,url):
            self.request_count+=1
            if url==INDEX:return HTML
            if url==PRIMARY:return DOC
            raise AssertionError('Unexpected URL '+url)
    with TemporaryDirectory() as temp:
        db_path=Path(temp)/'edgar.sqlite3'
        with connect(db_path) as c:
            add_form(c,filing());r=process_forms(c,Client(),db_path,limit=5)
            assert r['parsed']==1 and r['errors']==0
            assert process_forms(c,Client(),db_path,limit=5)['selected']==0
        subject=filing_subject(INDEX)
        parent=Path(temp)/'raw_pages'/'sec_filings'/subject
        for kind,body in [('index',HTML),('primary',DOC)]:
            files=list((parent/kind).glob('*.htm'))
            assert len(files)==1 and files[0].read_bytes()==body
            info=json.loads(files[0].with_suffix('.json').read_text())
            assert info['source_url']==(INDEX if kind=='index' else PRIMARY)
        assert accession_from_url(INDEX)==ACC

def test_migration_is_non_destructive_snapshot():
    # Small full v9 model, including duplicate Item types in different parts.
    with TemporaryDirectory() as tmp:
        from sec_edgar.migrate_v10 import upgrade_v9
        from news_pipeline.store import NEWS_TABLE, RELATION_TABLE
        db_path=Path(tmp)/'legacy.sqlite3'
        with sqlite3.connect(db_path) as conn:
            conn.executescript('''CREATE TABLE companies(cik TEXT PRIMARY KEY,name TEXT,ticker TEXT,exchange TEXT,source TEXT,created_at TEXT,updated_at TEXT);
             CREATE TABLE forms(accession TEXT PRIMARY KEY,cik TEXT,form_type TEXT,form_name TEXT,filed_date TEXT,filing_ts_utc TEXT,
                time_source TEXT,index_url TEXT,primary_url TEXT,file_guid TEXT,file_name TEXT,summary TEXT,status TEXT,error TEXT,created_at TEXT,updated_at TEXT);
             CREATE TABLE items(item_guid TEXT PRIMARY KEY,accession TEXT,file_guid TEXT,file_name TEXT,part TEXT,item_type TEXT,item_name TEXT,item_text TEXT,
                status TEXT,ai_impact INTEGER,ai_confidence REAL,ai_reason TEXT,ai_evidence TEXT,ai_monetary_effect REAL,
                ai_monetary_effect_reason TEXT,price_change_1w_pct REAL,price_change_reason TEXT,errors TEXT,created_at TEXT,updated_at TEXT);''')
            conn.execute("INSERT INTO companies VALUES('0000900075','Example',NULL,NULL,'SEC filing','a','b')")
            conn.execute('INSERT INTO forms VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ACC,'0000900075','10-Q','Quarterly','2026-10-16','2026-10-16T20:00:00Z',
                 'atom_updated',INDEX,None,None,None,'something','created',None,'a','b'))
            for n,part in enumerate(('PART I','PART II')):
                conn.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (f'guid{n}',ACC,None,None,part,'1','Title',f'Text{n}','analysed',2,7,'reason','quote',5,'basis',1,'price','', 'a','b'))
            conn.commit()
        with connect(db_path) as c:
            assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM forms').fetchone()[0]==1
            assert [x[0] for x in c.execute('SELECT item_occurrence FROM items ORDER BY id')]==[1,2]
            assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert len(list(Path(tmp).glob('legacy.pre-v10-*.sqlite3')))==1

def test_news_entity_link_uses_company_id_and_feed_is_archived():
    from datetime import datetime,timezone
    from sec_edgar.database import upsert_company
    from news_pipeline.store import ensure_schema,save_company_correlations
    from news_pipeline.matching import resolve_extracted_companies
    from news_pipeline.identity_validation import validate_resolved
    from news_pipeline.cli import process_news
    now=datetime(2026,10,17,14,0,tzinfo=timezone.utc)
    rss=b'''<?xml version="1.0"?><rss version="2.0"><channel><title>Example</title>
       <item><title>Example Issuer new contract</title><link>https://example.com/post</link>
       <description>Example Issuer signed an agreement.</description>
       <pubDate>Sat, 17 Oct 2026 13:30:00 GMT</pubDate></item></channel></rss>'''
    class FeedClient:
        def get(self,url):return rss
    with TemporaryDirectory() as temp:
        db_path=Path(temp)/'edgar.sqlite3'
        with connect(db_path) as c:
            ensure_schema(c)
            upsert_company(c,'0000900075','Example Issuer')
            result=process_news(c,FeedClient(),[{'name':'A Sample Feed','url':'https://example.com/feed','region':'US','category':'Business'}],now=now)
            assert result['inserted']==1
            news_id=c.execute('SELECT news_id FROM news').fetchone()[0]
            article=dict(c.execute('SELECT * FROM news WHERE news_id=?',(news_id,)).fetchone())
            extracted={news_id:[('Example Issuer','This issuer signed the announced agreement.')]}
            qualified=validate_resolved([article],resolve_extracted_companies(c,extracted))
            with c:
                c.execute("UPDATE news SET status='extracted' WHERE news_id=?",(news_id,))
                save_company_correlations(c,news_id,qualified[news_id])
            company_id=c.execute('SELECT id FROM companies WHERE cik=?',('0000900075',)).fetchone()[0]
            assert c.execute('SELECT company_id FROM news_company_relations').fetchone()[0]==company_id
            assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        folder=Path(temp)/'raw_pages'/'news_feeds'/'A_Sample_Feed'/'rss_xml'
        assert len(list(folder.glob('*.xml')))==1
        assert list(folder.glob('*.xml'))[0].read_bytes()==rss
