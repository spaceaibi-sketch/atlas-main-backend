"""Dashboard overview and page regression with exactly five physical tables.

These tests require no internet, no Dash package and no production data.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dashboard.canonical_reader import CanonicalConnection
from dashboard.overview_status import record_status, latest_runs, age_seconds, render_overview
from dashboard import explorer_queries, mobile_queries
from dashboard.data_quality import quality_counts
from dashboard.compact_summary import summary_counts
from dashboard.db_explorer_queries import browse, list_tables
from shared.canonical_schema import ensure_five_schema, table_names, REQUIRED


@pytest.fixture
def canonical(tmp_path):
    dbfile = tmp_path / 'atlas.sqlite3'
    db = sqlite3.connect(dbfile)
    db.row_factory = sqlite3.Row
    ensure_five_schema(db)
    timestamp = datetime(2026,10,25,12,0,0,tzinfo=timezone.utc)
    t = timestamp.isoformat().replace('+00:00','Z')
    old = (timestamp - timedelta(hours=1)).isoformat().replace('+00:00','Z')
    db.execute('INSERT INTO companies(id,cik,name,ticker,exchange,created_at,updated_at) VALUES(1,?,?,?,?,?,?)',
               ('0001','Company One','ONE','NYSE',t,t))
    rows = [
        (1,'filing','f:1',None,'8-K Filing','',None,old,old,'https://sec.gov/f','SEC','connected',{'id':10,'company_id':1,'form_type':'8-K','form_name':'Report'},old,t),
        (2,'item','i:1','f:1','Item 2.02','Report contents',None,old,old,'https://sec.gov/i','SEC','analysed',{'id':11,'form_id':10,'item_type':'2.02'},old,t),
        (3,'news','n:1',None,'News One','Article content','Summary',old,t,'https://news.example/n','Feed','connected',{},t,t),
        (4,'news','n:2',None,'News Two','Text','Summary',old,old,'https://news.example/n2','Feed','skipped',{},old,t),
    ]
    for row in rows:
        args=list(row)
        args[12]=json.dumps(args[12])
        db.execute('''INSERT INTO raw_items(id,source_type,source_key,parent_key,title,content,summary,
             event_at,received_at,source_url,source_name,status,metadata_json,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',args)
    for rid,src,impact in ((1,1,None),(2,2,None),(3,3,2)):
        db.execute('''INSERT INTO relationships(id,source_id,company_id,entity_type,entity_key,
           entity_name,ai_impact,metadata_json,created_at,updated_at)
           VALUES(?,?,1,'company','CIK:0001','Company One',?,'{}',?,?)''',(rid,src,impact,t,t))
    db.commit()
    yield CanonicalConnection(db), dbfile, timestamp
    db.close()


def test_overview_saved_ages_are_exact_and_five_table_only(canonical):
    db, _path, now = canonical
    result = record_status(db, now=now + timedelta(seconds=30))
    assert result['news']['total'] == 2
    assert result['news']['ages']['received'] == 30
    assert result['news']['ages']['connected'] == 30
    assert result['news']['ages']['skipped'] == 30
    assert result['news']['ages']['analysed'] is None
    assert result['item']['ages']['analysed'] == 30
    assert result['item']['ages']['received'] == 3630
    assert table_names(db) == REQUIRED
    assert age_seconds('invalid') is None
    assert age_seconds('2099-01-01T00:00:00Z',now) is None


def test_last_run_metrics_are_not_fabricated(tmp_path):
    path=tmp_path/'atlas_processing.log'
    entries = [
        dict(timestamp_utc='2026-10-25T12:00:00Z',stage='news.feed',action='run_completed',inserted=13),
        dict(timestamp_utc='2026-10-25T12:01:00Z',stage='news.ai',action='run_completed',main=8,connected_news=5),
        dict(timestamp_utc='2026-10-25T12:02:00Z',stage='news.scoring',action='run_completed',analysed_news=4),
        dict(timestamp_utc='2026-10-25T12:03:00Z',stage='sec.form',action='run_completed',items_saved=7),
        dict(timestamp_utc='2026-10-25T12:04:00Z',stage='ai.item',action='run_completed',analysed=3),
    ]
    path.write_text(''.join(json.dumps(e)+'\n' for e in entries))
    summary=latest_runs(path)
    assert summary['news']['received']['count'] == 13
    assert summary['news']['extracted']['count'] == 8
    assert summary['news']['connected']['count'] == 5
    assert summary['news']['analysed']['count'] == 4
    assert summary['item']['extracted']['count'] == 7
    assert summary['item']['analysed']['count'] == 3
    assert summary['item']['connected']['count'] is None
    assert summary['item']['connected']['at'] is None
    empty=latest_runs(tmp_path/'missing.log')
    assert empty['news']['analysed']['count'] is None


def test_all_page_queries_on_canonical_schema(canonical):
    db,path,now = canonical
    assert len(explorer_queries.companies(db)[0]) == 1
    assert len(explorer_queries.filings(db)[0]) == 1
    assert len(explorer_queries.items(db)[0]) == 1
    assert len(explorer_queries.news(db)[0]) == 2
    assert explorer_queries.filing_detail(db, 10)[0]['id'] == 10
    assert explorer_queries.item_detail(db,11)['item_text']=='Report contents'
    assert explorer_queries.news_detail(db,'n:1')[0]['title']=='News One'
    assert explorer_queries.company_detail(db,'0001')[0]['name']=='Company One'
    assert len(explorer_queries.recent_activity(db))==3
    assert mobile_queries.directory(db)[1]==1
    assert len(mobile_queries.related_forms(db,1))==1
    assert len(mobile_queries.related_items(db,1))==1
    assert len(mobile_queries.related_news(db,1))==1
    assert summary_counts(db)['news_all']==2
    assert quality_counts(db)['groups']['items']['total']==1
    assert list_tables(db)==['companies','raw_items','relationships','summary','price_bars']
    assert browse(db,'raw_items').rows
    # All of this is read-only; no new table or trigger may be created.
    assert table_names(db)==REQUIRED


def test_company_assessment_item_link_uses_legacy_display_id(canonical):
    db,path,now=canonical
    from dashboard.company_analytics import company_assessments
    rows=company_assessments(db,1,now=now)
    # A valid scored item should link to /items?item=11, not raw_items.id=2.
    db.execute("UPDATE relationships SET ai_impact=1 WHERE source_id=2")
    rows=company_assessments(db,1,now=now)
    assert next(r for r in rows if r['source_type']=='item')['source_id']=='11'


def test_operations_render_with_dash_component_stub(canonical,monkeypatch):
    from test_web_layout_smoke import Components
    import sys,types
    fake=types.SimpleNamespace(html=Components())
    monkeypatch.setitem(sys.modules,'dash',fake)
    db,path,now=canonical
    view=render_overview(record_status(db, now),latest_runs(path.parent/'missing.log'))
    assert len(view)==2
    from test_web_layout_smoke import nodes
    labels=' '.join(str(x.children) for x in nodes(view) if isinstance(x.children,str))
    assert 'News' in labels and 'Items' in labels
    assert 'No recorded run' in labels or '—' in labels


def test_dash_registered_pages_and_details_render(canonical, tmp_path, monkeypatch):
    from test_web_layout_smoke import app_module, nodes
    db, original, now = canonical
    with sqlite3.connect(tmp_path / 'web.sqlite3') as copy:
        db._db.backup(copy)
    app=app_module(monkeypatch,tmp_path)
    ids=[node.props.get('id') for node in nodes(app.app.layout)
         if 'id' in node.props]
    assert len(ids)==len(set(ids))
    assert 'overview-operations' in ids
    assert app.update_operations(0,'/')
    assert app.update_overview(0,'/',0)
    assert app.update_companies('','name',1,0,'/companies')[1].startswith('1 records')
    assert app.update_filings('','all','all',1,0,'/filings')[1].startswith('1 records')
    assert app.update_items('','all',1,0,'/items',0)[1].startswith('1 records')
    assert app.update_news('','all',1,0,'/news',0)[1].startswith('2 records')
    assert app.update_quality(0,'/quality',0)[1]
    for path,query in (('/companies','?cik=0001'),('/filings','?form=10'),
                       ('/items','?item=11'),('/news','?news=n%3A1')):
        result=app.load_record(path,query,0)
        assert len(result)==8
        assert 'has-detail' in result[4 + ('companies','filings','items','news').index(path[1:])]
    assert app.navigate('/mobile')[list(app.PATHS).index('mobile')]=={'display':'block'}
    assert app.navigate('/database')[list(app.PATHS).index('database')]=={'display':'block'}


def test_mobile_and_explorer_page_callbacks(canonical, tmp_path, monkeypatch):
    from test_web_layout_smoke import app_module
    db, path, now = canonical
    with sqlite3.connect(tmp_path / 'web.sqlite3') as copy:
        db._db.backup(copy)
    monkeypatch.setenv('ATLAS_DB_EXPLORER_TOKEN','test_admin_access')
    app = app_module(monkeypatch,tmp_path)
    callbacks = {fn.__name__: fn for _,_,fn in app.app.callbacks}
    mobile = callbacks['render_mobile']('/mobile','','','recent',1,'all',[],0)
    assert mobile[0]
    company = callbacks['render_mobile']('/mobile','?company=1&tab=items','', 'recent', 1, 'all', [], 0)
    assert company[0]
    unlocked = callbacks['unlock']('/database','test_admin_access',None)
    assert len(unlocked[0])==5
    schema = callbacks['show_schema']('raw_items','test_admin_access',None,None)
    assert schema[0] and schema[1]
    rows = callbacks['show_rows']('raw_items',None,'contains','','id','desc',1,0,
                                  'test_admin_access','/database')
    assert rows[0] and 'records' in rows[1]


def test_compact_summary_compares_utc_instants_not_wall_clocks(canonical):
    """The 3h summary stays correct for mixed timezone offsets and server clocks."""
    db, _path, now = canonical
    db.execute("UPDATE raw_items SET event_at=? WHERE id=3",
               ('2026-10-25T14:00:00+03:00',))   # 11:00Z; recent
    db.execute("UPDATE raw_items SET event_at=? WHERE id=4",
               ('2026-10-25T07:59:59-01:00',))   # 08:59Z; too old
    db.execute("UPDATE raw_items SET created_at=?, updated_at=? WHERE id=2",
               ('2026-10-25T13:40:00+02:00', '2026-10-25T13:45:00+02:00'))
    db.commit()
    a = summary_counts(db, now=now)
    b = summary_counts(db, now=now.astimezone(timezone(timedelta(hours=3))))
    assert a == b
    assert a['news_3h'] == 1
    assert a['last_news_published'] == '2026-10-25T11:00:00Z'
    assert a['items_extracted_3h'] == 1
    assert a['last_item_extracted'] == '2026-10-25T11:40:00Z'
    assert a['last_item_analysed'] == '2026-10-25T11:45:00Z'
    from dashboard.compact_summary import _timestamp
    assert _timestamp('2026-10-25T14:30:00+03:00') == '25 Oct 11:30 UTC'
    assert _timestamp('2026-10-25T06:30:00-05:00') == '25 Oct 11:30 UTC'


def test_compact_summary_rejects_naive_local_clock(canonical):
    db, _path, _now = canonical
    with pytest.raises(ValueError):
        summary_counts(db, now=datetime(2026, 10, 25, 12))
