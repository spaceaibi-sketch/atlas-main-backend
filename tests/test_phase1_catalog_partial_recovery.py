"""Regression tests: unlisted AI companies remain in news.companies without creating false SEC links."""
import sqlite3
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from news_pipeline.store import ensure_schema, pending_news
from news_pipeline.company_extraction import parse_lines
from news_pipeline.matching import identity_key
from news_pipeline.cli import process_ai

NOW = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)


def make_db(tmp_path):
    conn=sqlite3.connect(tmp_path / 'news.sqlite3')
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE companies (id INTEGER PRIMARY KEY, cik TEXT, name TEXT, ticker TEXT)')
    conn.execute("INSERT INTO companies VALUES(1, '0000012345', 'Apple Inc.', 'AAPL')")
    ensure_schema(conn)
    for i, headline in enumerate(('Apple Inc. opens new facility', 'Rivian Automotive, Inc. says something',
                                  'Article with no supported companies'), 1):
        conn.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,published_utc,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (f'news{i}',headline,f'https://example.org/{i}',f'url{i}',f'title{i}',f'story{i}',
             'source','2026-10-19T18:30:00+00:00','created','2026-10-19T18:35:00+00:00','2026-10-19T18:35:00+00:00'))
    conn.commit()
    return conn


class FakeAI:
    def __init__(self):
        self.responses=self
        self.calls=[]
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['text']['format']['type']=='json_schema':
            return SimpleNamespace(status='completed',output_text='{"groups": []}')
        rows=json.loads(kwargs['input'])['news']
        output=[]
        for article in rows:
            if article['title'].startswith('Apple'):
                output.append(f"{article['id']}|Apple Inc.|Apple Inc. opened a facility.")
            elif article['title'].startswith('Rivian'):
                output.append(f"{article['id']}|Rivian Automotive, Inc.|The company announced a new facility.")
            else:
                output.append(f"{article['id']}|NONE")
        return SimpleNamespace(status='completed',output_text='\n'.join(output))


def test_mixed_batch_preserves_unknown_name_and_skips_only_none(tmp_path):
    conn=make_db(tmp_path)
    ai=FakeAI()
    result=process_ai(conn, ai, now=NOW)
    assert result['errors']==0
    assert result['batches']==1
    assert result['no_company_skipped']==1
    assert result['skipped']==1
    assert result['extracted_companies']==2
    assert result['relations']==1
    assert result['catalog_unmatched']==1
    assert len(ai.calls)==2
    rows={r['news_id']:dict(r) for r in conn.execute('SELECT * FROM news')}
    assert rows['news1']['companies']=='Apple Inc.'
    assert rows['news1']['status']=='connected'
    assert rows['news2']['companies']=='Rivian Automotive, Inc.'
    assert rows['news2']['status']=='extracted'
    assert rows['news3']['companies']=='' and rows['news3']['status']=='skipped'
    assert conn.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==1
    assert not pending_news(conn)
    assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    conn.close()


def test_unlisted_identity_survives_alongside_a_valid_catalog_company():
    rejects=[]
    result=parse_lines('1|Rivian Automotive, Inc.|A non-catalogued identity.\n'
                       '1|Apple Inc.|Apple opens a facility.\n2|NONE',
                      {1:'n1',2:'n2'}, {identity_key('Apple Inc.'):'Apple Inc.'},
                      rejected_names=rejects)
    assert result == {'n1':[('Rivian Automotive, Inc.','A non-catalogued identity.'),
                             ('Apple Inc.','Apple opens a facility.')], 'n2':[]}
    assert rejects==[('n1','Rivian Automotive, Inc.')]


def test_legacy_empty_extracted_rows_are_skipped_but_valid_links_preserved(tmp_path):
    conn=make_db(tmp_path)
    stamp='2026-10-19T18:00:00+00:00'
    conn.execute("UPDATE news SET status='extracted',companies='' WHERE news_id='news1'")
    conn.execute("UPDATE news SET status='extracted',companies='' WHERE news_id='news2'")
    conn.execute("UPDATE news SET status='extracted',companies='Apple Inc.' WHERE news_id='news3'")
    conn.execute('''INSERT INTO news_company_relations
      (relation_id,news_id,company_id,entity_key,entity_type,entity_name,status,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?)''',('link','news1',1,'CIK:0000012345','company','Apple Inc.','connected',stamp,stamp))
    conn.commit()
    ensure_schema(conn)
    rows={r['news_id']:dict(r) for r in conn.execute('SELECT * FROM news')}
    assert rows['news1']['status']=='extracted'  # linked historical record protected
    assert rows['news2']['status']=='skipped'
    assert rows['news2']['filtration_reason']=='No qualifying companies extracted.'
    assert rows['news3']['status']=='extracted'
    assert rows['news3']['companies']=='Apple Inc.'
    assert (tmp_path/'news.sqlite3.before_empty_company_skip_v14.bak').exists()
    ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM news WHERE status='skipped'").fetchone()[0]==1
    conn.close()


def test_all_unmatched_names_remain_extracted_without_company_links(tmp_path):
    conn=make_db(tmp_path)
    # Preserve both proposed company names with their capitalization and input order.
    conn.execute("UPDATE news SET status='skipped' WHERE news_id='news1'")
    conn.execute("UPDATE news SET status='skipped' WHERE news_id='news3'")
    conn.commit()
    class UnlistedAI(FakeAI):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs['text']['format']['type']=='json_schema':
                return SimpleNamespace(status='completed',output_text='{"groups": []}')
            return SimpleNamespace(status='completed',output_text='1|Rivian Automotive, Inc.|An announced project.\n1|Example Unlisted Holdings Ltd.|A named counterparty.')
    # A single isolated created article is deferred by design; add a reference.
    conn.execute("UPDATE news SET status='extracted', companies='Apple Inc.' WHERE news_id='news1'")
    conn.commit()
    results=process_ai(conn,UnlistedAI(),now=NOW)
    assert results['errors']==0
    assert results['catalog_unmatched']==2
    assert results['no_company_skipped']==0
    row=conn.execute("SELECT status,companies FROM news WHERE news_id='news2'").fetchone()
    assert row['status']=='extracted'
    assert row['companies']=='Rivian Automotive, Inc.|Example Unlisted Holdings Ltd.'
    assert conn.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==0
    conn.close()
