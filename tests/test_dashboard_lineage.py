"""Dash lineage counts must reconcile without mixing articles and relations."""
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from dashboard.lineage import news_lineage
from news_pipeline.store import ensure_schema, add_story
from news_pipeline.feeds import Story
from sec_edgar.database import connect, upsert_company

NOW = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)


def add(db, label, *, age_minutes=10, batch='pending', status='created'):
    stamp = (NOW - timedelta(minutes=age_minutes)).isoformat()
    row_id = add_story(db, Story(label, f'https://example.com/{label}',
                                 label,stamp,'Sample','US','News'))[1]
    with db:
        db.execute("UPDATE news SET status=?,filtration_reason=? WHERE news_id=?",
                   ('extracted' if batch=='main' and status=='created' else status,
                    'Duplicate coverage; skipped.' if batch=='duplicate' else '',row_id))
    return row_id


def link(db,news_id,key,name,status='connected',company_id=None,entity_type='company'):
    with db:
        db.execute('''INSERT INTO news_company_relations
            (relation_id, news_id, company_id, entity_key, entity_type, entity_name,
             status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
             (news_id+'-'+key,news_id,company_id,key,entity_type,name,status,
              NOW.isoformat(),NOW.isoformat()))


def test_lineage_all_time_and_last_three_hours_have_consistent_denominators(tmp_path):
    db=connect(tmp_path/'stats.sqlite3')
    ensure_schema(db)
    with db:
        a=upsert_company(db,'0000000091','Company A','A')
        b=upsert_company(db,'0000000092','Company B','B')

    n1=add(db,'one',batch='main',status='analysed')
    link(db,n1,'a','Company A',status='analysed',company_id=a)
    link(db,n1,'b','Company B',status='analysed',company_id=b)
    n2=add(db,'two',batch='main',status='connected')
    link(db,n2,'a','Company A',company_id=a)
    n3=add(db,'three',batch='main',status='created')
    n4=add(db,'four',batch='duplicate',status='skipped')
    n5=add(db,'five',batch='pending',status='created')
    n6=add(db,'six',batch='pending',status='skipped')
    n7=add(db,'seven',batch='main',status='connected')
    link(db,n7,'government','Saudi Government',status='analysed',entity_type='government')
    n8=add(db,'eight',age_minutes=400,batch='main',status='analysed')
    link(db,n8,'a','Company A',status='analysed',company_id=a)
    n9=add(db,'nine',age_minutes=400,batch='pending',status='skipped')
    link(db,n9,'legacy','Legacy Government',entity_type='government')

    r=news_lineage(db,scope='3h',now=NOW)
    assert (r['stored'],r['main'],r['duplicates'],r['ungrouped'])==(7,4,1,2)
    assert r['ungrouped_skipped']==1
    assert (r['main_linked'],r['main_unlinked'])==(3,1)
    assert (r['main_complete'],r['main_incomplete'])==(1,2)
    assert (r['main_links'],r['main_scored_links'])==(4,3)
    assert (r['company_links'],r['other_links'],r['distinct_companies'])==(3,1,2)
    assert r['nonmain_links']==0
    assert (r['created'],r['skipped'],r['extracted'],r['connected'],r['analysed'])==(1,2,1,2,1)

    r=news_lineage(db,scope='all',now=NOW)
    assert (r['stored'],r['main'],r['duplicates'],r['ungrouped'])==(9,5,1,3)
    assert (r['main_linked'],r['main_unlinked'])==(4,1)
    assert (r['main_complete'],r['main_incomplete'])==(2,2)
    assert (r['relations'],r['company_links'],r['other_links'])==(6,4,2)
    assert (r['main_links'],r['main_scored_links'],r['nonmain_links'])==(5,4,1)
    assert r['distinct_companies']==2
    assert (r['created'],r['skipped'],r['extracted'],r['connected'],r['analysed'])==(1,3,1,2,2)
    for group in ('all','3h'):
        value=news_lineage(db,scope=group,now=NOW)
        assert value['stored']==value['main']+value['duplicates']+value['ungrouped']
        assert value['stored']==value['created']+value['skipped']+value['extracted']+value['connected']+value['analysed']
        assert value['main']==value['main_linked']+value['main_unlinked']
        assert value['main_linked']==value['main_complete']+value['main_incomplete']
        assert value['relations']==value['company_links']+value['other_links']
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    db.close()


def test_lineage_empty_database_and_invalid_scope(tmp_path):
    db=connect(tmp_path/'empty.sqlite3')
    ensure_schema(db)
    for scope in ('all','3h'):
        r=news_lineage(db,scope,now=NOW)
        assert r['stored']==r['relations']==r['main_complete']==0
    with pytest.raises(ValueError):
        news_lineage(db,'an-unknown-scope',now=NOW)
    db.close()


def test_dashboard_scope_and_sections_wired():
    app_source=(Path(__file__).resolve().parents[1]/'app.py').read_text()
    assert 'def article_panel' in app_source
    assert 'def render_news_lineage' in Path(__file__).resolve().parents[1].joinpath('dashboard/lineage.py').read_text()
    assert 'def article_panel' in app_source
    assert 'news_lineage' in Path(__file__).resolve().parents[1].joinpath('dashboard/lineage.py').read_text()
