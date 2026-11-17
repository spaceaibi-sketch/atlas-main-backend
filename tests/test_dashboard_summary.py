"""Regression coverage for the six rows in the compact top summary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import types

from dashboard.compact_summary import summary_counts, render_summary
from sec_edgar.database import connect, upsert_company
from news_pipeline.store import ensure_schema, add_story
from news_pipeline.feeds import Story

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc)


def stamp(min_ago):
    return (NOW - timedelta(minutes=min_ago)).isoformat(timespec='seconds').replace('+00:00', 'Z')


def insert_news(db, label, age, status):
    key = add_story(db, Story(label, 'https://example.net/'+label,
                              label, stamp(age), 'Test','US','Test'))[1]
    with db:
        db.execute('UPDATE news SET status=?,updated_at=? WHERE news_id=?',
                   (status,stamp(age-1),key))


def insert_form(db, company_id, suffix, age, status):
    with db:
        return db.execute('''INSERT INTO forms
            (company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
            VALUES (?,'8-K','Current report',?,?,?, ?,?)''',
            (company_id,stamp(age),f'https://www.sec.gov/Archives/{suffix}',
             status,stamp(age),stamp(age))).lastrowid


def insert_item(db, form_id, kind, status, text, age):
    with db:
        db.execute('''INSERT INTO items
            (form_id,item_type,item_name,item_text,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)''',
            (form_id,kind,'Item '+kind,text,status,stamp(age),stamp(age)))


def test_summary_counts_all_and_three_hour_windows(tmp_path):
    db=connect(tmp_path/'summary.db')
    ensure_schema(db)
    with db:
        company=upsert_company(db,'0000000065','Company Alpha')
        upsert_company(db,'0000000066','Company Beta')

    insert_news(db,'recent_connected',10,'connected')
    insert_news(db,'recent_skipped',20,'skipped')
    insert_news(db,'recent_analysed',30,'analysed')
    insert_news(db,'old_connected',250,'connected')
    insert_news(db,'future_article',-120,'created')
    recent=insert_form(db,company,'recent',10,'parsed')
    old=insert_form(db,company,'old',250,'parsed')
    insert_form(db,company,'recent_error',40,'error')
    insert_item(db,recent,'1.01','processed','Agreement text',5)
    insert_item(db,recent,'8.01','analysed','Other event',4)
    insert_item(db,recent,'5.02','rejected','Rejected for this event',6)
    insert_item(db,old,'1.01','processed','Old agreement text',200)

    s=summary_counts(db,now=NOW)
    assert s['companies']==2
    assert [s[k] for k in ('news_all','news_skipped_all','news_connected_all','news_analysed_all')]==[5,1,2,1]
    assert [s[k] for k in ('news_3h','news_skipped_3h','news_connected_3h','news_analysed_3h')]==[3,1,1,1]
    assert [s[k] for k in ('items_extracted_all','items_parsed_all','items_analysed_all')]==[4,3,1]
    assert [s[k] for k in ('items_extracted_3h','items_parsed_3h','items_analysed_3h')]==[3,2,1]
    assert s['last_news_published']==stamp(10)
    assert s['last_news_analysed']==stamp(29)
    assert s['last_item_analysed']==stamp(4)
    assert s['last_item_extracted']==stamp(4)
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    db.close()



def test_three_hour_item_counts_follow_extraction_not_sec_filing_time(tmp_path):
    db=connect(tmp_path/'extraction_window.db')
    ensure_schema(db)
    with db:
        company=upsert_company(db,'0000000155','Time Window Corp')
    recent_filing=insert_form(db,company,'recent_filing',5,'parsed')
    old_filing=insert_form(db,company,'old_filing',300,'parsed')
    # A newly extracted Item from an old filing belongs in the 3h Item counts.
    insert_item(db,old_filing,'1.01','processed','Verified full text',12)
    # An older extracted Item from a recent filing is NOT in the 3h Item counts,
    # even if its AI status changed recently.
    insert_item(db,recent_filing,'8.01','analysed','Old text',301)
    with db:
        db.execute('UPDATE items SET updated_at=? WHERE item_type=?',(stamp(3),'8.01'))
    s=summary_counts(db,now=NOW)
    assert (s['items_extracted_all'],s['items_parsed_all'],s['items_analysed_all'])==(2,2,1)
    assert (s['items_extracted_3h'],s['items_parsed_3h'],s['items_analysed_3h'])==(1,1,0)
    db.close()


def test_parsed_requires_nonempty_text_and_valid_current_status(tmp_path):
    db=connect(tmp_path/'quality.db')
    ensure_schema(db)
    with db:
        company=upsert_company(db,'0000000195','Quality Corp')
    filing=insert_form(db,company,'recent',15,'parsed')
    insert_item(db,filing,'1.01','processed','   ',12)
    insert_item(db,filing,'1.02','rejected','Nonempty but rejected',12)
    insert_item(db,filing,'1.03','analysed','Verified',12)
    s=summary_counts(db,now=NOW)
    assert (s['items_extracted_all'],s['items_parsed_all'],s['items_analysed_all'])==(3,1,1)
    assert (s['items_extracted_3h'],s['items_parsed_3h'],s['items_analysed_3h'])==(3,1,1)
    db.close()


def test_summary_counts_empty_database(tmp_path):
    db=connect(tmp_path/'empty.db')
    ensure_schema(db)
    s=summary_counts(db,now=NOW)
    assert s['news_all']==s['items_extracted_all']==s['items_parsed_all']==s['items_analysed_all']==s['companies']==0
    assert s['news_3h']==s['items_extracted_3h']==s['items_parsed_3h']==s['items_analysed_3h']==0
    assert not s['last_news_published'] and not s['last_news_analysed'] and not s['last_item_analysed']
    assert not s['last_item_extracted']
    db.close()


class FakeHtml:
    def __getattr__(self,name):
        def component(*args,**kwargs):
            return {'tag':name,'children':args[0] if args else kwargs.get('children'), 'props':kwargs}
        return component


def flatten(node):
    if isinstance(node,dict):
        yield node
        yield from flatten(node['children'])
    elif isinstance(node,(list,tuple)):
        for item in node:
            yield from flatten(item)


def texts(node):
    if isinstance(node,str):
        yield node
    elif isinstance(node,dict):
        yield from texts(node['children'])
    elif isinstance(node,(list,tuple)):
        for item in node:
            yield from texts(item)


def test_summary_is_six_flat_rows_no_controls(monkeypatch):
    monkeypatch.setitem(sys.modules,'dash',types.SimpleNamespace(html=FakeHtml()))
    fields={'companies':8077,'news_all':766,'news_skipped_all':6,'news_connected_all':16,
            'news_analysed_all':0,'news_3h':39,'news_skipped_3h':6,'news_connected_3h':16,
            'news_analysed_3h':0,'items_extracted_all':883,'items_parsed_all':700,
            'items_analysed_all':500,'items_extracted_3h':12,'items_parsed_3h':10,'items_analysed_3h':8,
            'last_news_published':stamp(12),'last_news_analysed':None,'last_item_analysed':stamp(20),
            'last_item_extracted':stamp(14)}
    view=render_summary(fields)
    tiles=[n for n in flatten(view) if 'summary-tile' in n['props'].get('className','').split()]
    assert len(tiles)==6
    body=' '.join(texts(view))
    for label in ('Companies','News · Total','News · 3h','Items · Total','Items · 3h',
                  'Latest · UTC','Extracted','Parsed','Analysed',
                  'News published','News analysed','Item extracted','Item analysed'):
        assert label in body
    assert '8,077' in body and '883' in body and '766' in body
    assert '500' in body and '8' in body
    assert not [c for c in flatten(view) if c['tag'] in ('Details','Summary','RadioItems')]


def test_summary_occupies_ten_percent_only_on_quality():
    root=Path(__file__).resolve().parents[1]
    source=(root/'app.py').read_text()
    css=(root/'assets'/'style.css').read_text()
    assert "id='top-summary'" not in source
    assert "id='quality-summary'" in source
    assert source.index("id='page-quality'")<source.index("id='quality-summary'")<source.index("id='quality-content'")
    assert "Input('ai-revision','data')" in source
    assert '.quality-summary{flex:0 0 10dvh;' in css
    assert "if current_page(path) != 'quality':" in source
    assert 'grid-template-rows:repeat(2,minmax(0,1fr))' in css
    assert "id='page-companies'" not in source or "workspace('companies'" in source
    assert "id='lineage-window'" not in source
