"""Dashboard regression: no news, forms, items, or news_company_relations tables."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
import pytest

from dashboard.unified_read_adapter import install_dashboard_views, five_table_only
from dashboard.db_explorer_queries import browse, list_tables
from dashboard import explorer_queries as x
from dashboard import mobile_queries as m
from dashboard.company_analytics import company_assessments, refresh_company_aggregates
from dashboard.compact_summary import summary_counts
from dashboard.data_quality import quality_counts
from dashboard.lineage import news_lineage

@pytest.fixture
def db(tmp_path):
    path=tmp_path/'atlas.sqlite3'
    writer=sqlite3.connect(path)
    writer.row_factory=sqlite3.Row
    # Exact five canonical tables, with all fields used by dashboard.
    from shared.unified_model import SCHEMA as UNIFIED
    writer.execute('''CREATE TABLE companies(id INTEGER PRIMARY KEY,cik TEXT,name TEXT,ticker TEXT,exchange TEXT,
         acumm_impact REAL DEFAULT 0,acumm_monetary REAL DEFAULT 0,accum_confdent REAL DEFAULT 0,
         description TEXT,created_at TEXT,updated_at TEXT)''')
    writer.executescript(UNIFIED)
    # Simulate removal of migration-only relationship identifiers.
    writer.execute('DROP INDEX IF EXISTS idx_rel_legacy_news')
    writer.execute('DROP INDEX IF EXISTS idx_rel_legacy_item')
    writer.execute('ALTER TABLE relationships DROP COLUMN legacy_relation_id')
    writer.execute('ALTER TABLE relationships DROP COLUMN legacy_item_id')
    writer.execute('''CREATE TABLE summary(company_id INTEGER PRIMARY KEY, aggregate_score REAL,
       latest_published TEXT,latest_filing TEXT,last_price REAL,trend_24h REAL,trend_7d REAL,refreshed_at TEXT)''')
    writer.execute('''CREATE TABLE price_bars(company_id INTEGER,bar_time_utc TEXT,close REAL,open REAL,
       high REAL,low REAL,volume INTEGER,feed TEXT,PRIMARY KEY(company_id,bar_time_utc))''')
    t=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    writer.execute('INSERT INTO companies VALUES (1,?,?,?,?,?,?,?,?,?,?)',(
         '0001','Atlas Testing','ATLS','NYSE',0,0,0,'Testing',t,t))
    writer.execute('INSERT INTO summary VALUES (1,10,?,?,100,1,2,?)',(t,t,t))
    writer.execute('INSERT INTO price_bars VALUES (1,?,100,100,102,99,1000,?)',(t,'iex'))
    raw=[
      (1,'news','article-1',None,'Headline','Article full text','Summary',t,t,
       'https://example.com/a','Example Press','analysed',{'title_key':'header','url_key':'url1','story_key':'story1','region':'US','category':'business','filtration_reason':'Relevant'},t,t),
      (2,'filing','10',None,'8-K Current Report','',None,t,t,'https://sec.gov/f/10','SEC','connected',
       {'id':10,'company_id':1,'form_type':'8-K','form_name':'Current Report','primary_url':'https://sec.gov/report10','file_guid':'aaaa','file_name':'aaaa.htm'},t,t),
      (3,'item','11','https://sec.gov/f/10','Item 2.02','Quarterly results',None,t,t,'https://sec.gov/report10','8-K','analysed',
       {'id':11,'form_id':10,'item_type':'2.02','item_occurrence':1},t,t),
    ]
    for a in raw:
       vals=list(a);vals[12]=json.dumps(vals[12]);writer.execute('''INSERT INTO raw_items
          (id,source_type,source_key,parent_key,title,content,summary,event_at,received_at,source_url,
           source_name,status,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',vals)
    writer.execute('''INSERT INTO relationships(source_id,company_id,entity_type,entity_key,entity_name,
       ai_impact,ai_confidence,ai_monetary_effect,ai_reason,ai_evidence,metadata_json,
       created_at,updated_at) VALUES (1,1,'company','company:1','Atlas Testing',3,9,500,
       'Major product','Source',?, ?, ?)''',(json.dumps({'relation_id':'r1'}),t,t))
    writer.execute('''INSERT INTO relationships(source_id,company_id,entity_type,entity_key,entity_name,
       metadata_json,created_at,updated_at) VALUES (2,1,'company','company:1','Atlas Testing','{}',?,?)''',(t,t))
    writer.execute('''INSERT INTO relationships(source_id,company_id,entity_type,entity_key,entity_name,
       ai_impact,ai_confidence,ai_monetary_effect,ai_reason,ai_evidence,metadata_json,
       created_at,updated_at) VALUES (3,1,'company','company:1','Atlas Testing',-1,7,-200,
       'Disclosure','Filing',?, ?, ?)''',('{}',t,t))
    writer.commit()
    try:
      assert five_table_only(writer)
      reader=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
      reader.row_factory=sqlite3.Row
      assert install_dashboard_views(reader)
      yield reader,writer
      reader.close()
    finally:
      writer.close()


def test_legacy_projections_are_temp_and_physical_five(db):
    reader,_=db
    assert set(list_tables(reader))=={'companies','raw_items','relationships','summary','price_bars'}
    assert len(reader.execute('SELECT * FROM news').fetchall())==1
    assert len(reader.execute('SELECT * FROM forms').fetchall())==1
    assert len(reader.execute('SELECT * FROM items').fetchall())==1
    assert len(reader.execute('SELECT * FROM news_company_relations').fetchall())==1
    assert reader.execute('SELECT COUNT(*) FROM sqlite_master WHERE name IN (\'news\',\'forms\',\'items\',\'news_company_relations\')').fetchone()[0]==0
    records=browse(reader,'raw_items',order='id',direction='asc')
    assert records.rows[0]['Linked companies']=='Atlas Testing (ID 1)'
    price=browse(reader,'price_bars')
    assert price.rows[0]['close']=='100.0'


def test_company_filing_news_and_item_details(db):
    reader,_=db
    assert x.companies(reader)[1]==1
    assert x.filings(reader)[1]==1
    assert x.items(reader)[1]==1
    assert x.news(reader)[1]==1
    assert x.company_detail(reader,'0001')[0]['name']=='Atlas Testing'
    assert x.filing_detail(reader,10)[0]['form_type']=='8-K'
    assert x.item_detail(reader,11)['ai_impact']==-1
    assert x.news_detail(reader,'article-1')[1][0]['ai_impact']==3
    assert len(x.recent_activity(reader))==3


def test_dash_mobile_overview_and_quality(db):
    reader,_=db
    assert m.directory(reader)[1]==1
    assert m.company(reader,1)['name']=='Atlas Testing'
    assert m.assessment_summary(reader,1)['scored_news']==1
    assert len(m.related_news(reader,1))==1
    assert len(m.related_items(reader,1))==1
    assert len(m.related_forms(reader,1))==1
    assert m.company_record_count(reader,1,'news')==1
    assert len(m.price_bars(reader,1))==1
    stats=summary_counts(reader)
    assert stats['news_all']==1 and stats['items_analysed_all']==1
    assert quality_counts(reader)['groups']['news']['total']==1
    assert news_lineage(reader)['stored']==1


def test_company_analytics_can_write_without_legacy_tables(db):
    reader,writer=db
    assert len(company_assessments(reader,1))==2
    refresh_company_aggregates(writer)
    assert writer.execute('SELECT acumm_impact FROM companies WHERE id=1').fetchone()[0]!=0
    assert five_table_only(writer)


def test_dash_callbacks_and_explorer_on_five_table_database(db, tmp_path, monkeypatch):
    from test_web_layout_smoke import app_module
    _, writer=db
    with sqlite3.connect(tmp_path/'web.sqlite3') as backup:
        writer.backup(backup)
    dash=app_module(monkeypatch,tmp_path)
    assert dash.FIVE_TABLE_MODE
    assert five_table_only(dash.db_read())
    assert dash.update_overview(0,'/',0)
    assert dash.update_companies('','name',1,0,'/companies')[1].startswith('1 records')
    assert dash.update_filings('','all','all',1,0,'/filings')[1].startswith('1 records')
    assert dash.update_items('','all',1,0,'/items',0)[1].startswith('1 records')
    assert dash.update_news('','all',1,0,'/news',0)[1].startswith('1 records')
    assert dash.update_quality(0,'/quality',0)[1]
    assert len(dash.load_record('/items','?item=11',0))==8
    assert len(dash.load_record('/news','?news=article-1',0))==8
    assert len(dash.load_record('/filings','?form=10',0))==8
    assert len(dash.load_record('/companies','?cik=0001',0))==8
    assert dash.show_item_actions('/items','?item=11')=={'display':'none'}
    assert dash.run_selected_analysis(1,'/items','?item=11',0)[0].startswith('The legacy')
    with dash.db_read() as physical:
        assert list_tables(physical)==['companies','raw_items','relationships','summary','price_bars']
