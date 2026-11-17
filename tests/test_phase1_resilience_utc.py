"""Phase 1 robustness: recover harmless formatting noise, reject unsafe data.

All tests use supplied fake AI responses, not live API calls.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from news_pipeline.batch_filter import analyze_batch
from news_pipeline.company_extraction import parse_lines
from news_pipeline.matching import identity_key
from news_pipeline.feeds import utc_time
from news_pipeline.store import ensure_schema, pending_news, expire_pending
from news_pipeline.time_window import publication_status, ensure_utc_now, read_utc


class Fake:
    def __init__(self, text):
        self.text = text
        self.responses = self
    def create(self, **kwargs):
        return SimpleNamespace(status='completed', output_text=self.text)


ROWS = [{'news_id':'n1', 'title':'one', 'summary':'first'}, {'news_id':'n2', 'title':'two', 'summary':'second'}]


@pytest.mark.parametrize('text', [
    '```json\n{"groups":[["1","2"],],}\n```',
    'Here is the output:\n{"groups":[[1,2]]}\nDone.',
    '{"groups":[[1,2]]}',
])
def test_recover_unambiguous_duplicate_groups(text):
    mains, duplicates, groups = analyze_batch(Fake(text), ROWS)
    assert mains == ['n1']
    assert duplicates == {'n2':'n1'}
    assert groups == 1


def test_singleton_group_does_not_create_duplicate():
    assert analyze_batch(Fake('{"groups":[[1],[2]]}'), ROWS) == (['n1','n2'], {}, 0)


@pytest.mark.parametrize('text', [
    '{"groups":[[1,3]]}',
    '{"groups":[[1,2],[2,1]]}',
    '{"groups":[[1,2]]}{"groups":[]}',
    '{"groups":[["xx",2]]}',
    'arbitrary broken data',
])
def test_unsafe_group_corruptions_rejected(text):
    with pytest.raises(ValueError):
        analyze_batch(Fake(text), ROWS)


def test_company_response_recovers_markdown_bullets_and_duplicates():
    raw = '```text\n- 1 | Apple Inc. | Company announced its own product.\n- 1|Apple Inc.|Company announced its own product.\n2 | none\n```'
    actual = parse_lines(raw, {1:'n1',2:'n2'}, {identity_key('Apple Inc.'):'Apple Inc.'})
    assert actual == {'n1':[('Apple Inc.','Company announced its own product.')], 'n2':[]}


@pytest.mark.parametrize('text', [
    '1|NONE',  # a missing story cannot be invented
    '1|Apple Inc.|Reason A\n1|Apple Inc.|Reason B\n2|NONE',
    '1|NONE\n1|Apple Inc.|Reason\n2|NONE',
])
def test_unsafe_company_output_fails_closed(text):
    with pytest.raises(ValueError):
        parse_lines(text,{1:'n1',2:'n2'}, {identity_key('Apple Inc.'):'Apple Inc.'})


def test_utc_news_timestamp_handling():
    now = datetime(2026,10,19,12,0,0,tzinfo=timezone.utc)
    # Offset-aware feed timestamps all compare in UTC.
    assert publication_status('2026-10-19T14:00:00+03:00',now)=='fresh'
    assert publication_status('2026-10-19T07:59:59Z',now)=='outside_window'
    assert publication_status('2026-10-19T15:01:00+03:00',now)=='future'
    assert publication_status('2026-10-19T11:00:00',now)=='missing_time'
    assert read_utc('2026-10-19T14:00:00+03:00') == datetime(2026,10,19,11,tzinfo=timezone.utc)
    assert utc_time('Mon, 19 Oct 2026 14:00:00 +0300')=='2026-10-19T11:00:00+00:00'
    assert utc_time('2026-10-19T11:00:00')==''
    with pytest.raises(ValueError,match='timezone-aware'):
        ensure_utc_now(datetime(2026,10,19,12))


def test_queued_news_does_not_expire_from_utc_three_hour_window():
    now=datetime(2026,10,19,12,tzinfo=timezone.utc)
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    # Only the minimal required company table if relation FK is declared.
    conn.execute('CREATE TABLE companies(id INTEGER PRIMARY KEY)')
    ensure_schema(conn)
    conn.execute("""INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,published_utc,status,created_at,updated_at)
                 VALUES('n1','Title','https://example.com','url','title','story','feed',?, 'created',?,?)""",
                 ((now-timedelta(hours=7)).isoformat(),now.isoformat(),now.isoformat()))
    conn.commit()
    assert expire_pending(conn,now)==0
    assert [x['news_id'] for x in pending_news(conn,now=now,enforce_window=True)]==['n1']
    assert conn.execute("SELECT status FROM news WHERE news_id='n1'").fetchone()['status']=='created'
    conn.close()


def test_old_created_articles_are_still_processed_after_ai_delay():
    from news_pipeline.cli import process_ai

    now=datetime(2026,10,19,12,tzinfo=timezone.utc)
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE companies(id INTEGER PRIMARY KEY, name TEXT, ticker TEXT, cik TEXT)')
    conn.execute("INSERT INTO companies(id,name,ticker,cik) VALUES(1,'Apple Inc.','AAPL','0000001')")
    ensure_schema(conn)
    for idx in (1,2):
        conn.execute("""INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,published_utc,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?, 'created',?,?)""",
                    (f'n{idx}',f'Title {idx}',f'https://example.com/{idx}',f'url{idx}',f'title{idx}',f'story{idx}',
                     'feed',(now-timedelta(hours=7)).isoformat(),now.isoformat(),now.isoformat()))
    conn.commit()

    class Phase1Fake:
        def __init__(self):
            self.responses=self
            self.calls=0
        def create(self,**request):
            self.calls+=1
            if request['text']['format']['type']=='json_schema':
                return SimpleNamespace(status='completed',output_text='```json\n{"groups":[],}\n```')
            return SimpleNamespace(status='completed',output_text='1 | NONE\n2 | NONE')

    api=Phase1Fake()
    totals=process_ai(conn,api,now=now)
    assert totals['errors']==0 and totals['batches']==1 and totals['stale_skipped']==0
    assert api.calls==2
    assert [r['status'] for r in conn.execute('SELECT status FROM news ORDER BY news_id')]==['skipped','skipped']
    conn.close()
