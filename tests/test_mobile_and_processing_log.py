"""New Dash workspace SQL scoping and append-only unified log contract."""
from __future__ import annotations

import json
from datetime import datetime,timezone
from pathlib import Path

from dashboard.mobile_queries import company,directory,assessment_summary,related_news,related_items,related_forms,price_bars
from news_pipeline.store import add_story,ensure_schema,save_company_correlations
from news_pipeline.feeds import Story
from price_pipeline.storage import ingest_bars
from sec_edgar.database import connect,upsert_company
from shared.processing_log import log_step


def test_log_is_json_lines_in_one_configured_file(tmp_path,monkeypatch):
    target=tmp_path/'atlas_processing.log'
    monkeypatch.setenv('ATLAS_LOG_PATH',str(target))
    log_step('sec.item','stored',form_id=1,item_id=9,item_type='1.01')
    log_step('news.linking','relation_proposed',news_id='n-a',correlation_reason='Article names Company A')
    log_step('price.bar','batch_completed',company_id=11,saved=2)
    records=[json.loads(x) for x in target.read_text().splitlines()]
    assert len(records)==3
    assert [x['stage'] for x in records]==['sec.item','news.linking','price.bar']
    assert records[0]['item_id']==9
    assert records[1]['correlation_reason']=='Article names Company A'
    assert all('timestamp_utc' in x and x['pid'] for x in records)
    assert set(tmp_path.glob('*.log'))=={target}


def test_mobile_directory_and_company_research_are_scoped(tmp_path,monkeypatch):
    monkeypatch.setenv('ATLAS_LOG_PATH',str(tmp_path/'all.jsonl'))
    db=connect(tmp_path/'test.sqlite3')
    ensure_schema(db)
    a=upsert_company(db,'0000000045','ALPHA CORPORATION','ALP','NYSE')
    b=upsert_company(db,'0000000046','BETA CORPORATION','BET','NASDAQ')
    title='Alpha announces an agreement'
    news_id=add_story(db,Story(title,'https://example.org/alpha',title,
        '2026-10-19T10:00:00+00:00','Demo RSS','US','Company'))[1]
    with db:
        db.execute("UPDATE news SET status='extracted' WHERE news_id=?",(news_id,))
        save_company_correlations(db,news_id,[(a,'0000000045','ALPHA CORPORATION','A named contract')])
    bar={'t':'2026-10-19T09:00:00Z','o':10.0,'h':11.0,'l':9.0,'c':10.5,'v':100,'n':2,'vw':10.2}
    assert ingest_bars(db,a,[bar],datetime(2026,10,19,8,tzinfo=timezone.utc),
                       datetime(2026,10,19,10,tzinfo=timezone.utc),'sip',now=datetime(2026,10,19,10,tzinfo=timezone.utc))==1
    rows,total,page=directory(db,'ALP','name',1)
    assert total==1 and page==1 and rows[0]['id']==a and rows[0]['last_price']==10.5
    rows,total,_=directory(db,'','name',1,[b],True)
    assert total==1 and rows[0]['id']==b
    assert company(db,a)['last_price']==10.5
    assert related_news(db,b)==[]
    related=related_news(db,a)
    assert len(related)==1 and related[0]['correlation_reason']=='A named contract'
    assert assessment_summary(db,a)['scored_news']==0  # linked but not assessed
    assert related_items(db,a)==[]
    assert related_forms(db,a)==[]
    assert price_bars(db,a)[0]['close']==10.5
    db.close()


def test_mobile_page_is_additive_and_quality_unchanged(monkeypatch,tmp_path):
    from test_web_layout_smoke import app_module,nodes
    m=app_module(monkeypatch,tmp_path)
    ids={n.props.get('id') for n in nodes(m.app.layout)}
    assert {'page-mobile','mobile-body','mobile-controls','mobile-favorites','mobile-theme',
            'mobile-fav-toggle','mobile-search','mobile-watchlist-mode'}.issubset(ids)
    assert 'page-quality' in ids and 'quality-summary' in ids and 'quality-content' in ids
    assert m.current_page('/mobile')=='mobile'
    assert 'nav-link active' in m.navigate('/mobile')  # mobile navigation remains active
    assert len(m.app.callbacks)>=14
