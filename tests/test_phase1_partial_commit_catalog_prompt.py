"""Per-article Phase-1 recovery and catalog-name contract (no network calls)."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sec_edgar.database import connect
from news_pipeline.cli import process_ai
from news_pipeline.store import ensure_schema
from news_pipeline.company_extraction import parse_lines
from news_pipeline.matching import identity_key
from news_pipeline.batch_filter import analyze_batch
from news_pipeline.company_extraction import PROMPT

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)


def fixture(tmp_path, count=4):
    db = connect(tmp_path/'news.sqlite3')
    ensure_schema(db)
    with db:
        db.execute("INSERT INTO companies(cik,name,ticker,created_at,updated_at) "
                   "VALUES('0001000001','AMAZON COM INC','AMZN','now','now')")
        for i in range(1, count + 1):
            db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,
                published_utc,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (f'n{i}', f'Example article {i}', f'https://news.test/{i}',
                 f'url{i}',f'title{i}',f'story{i}','Publisher',f'2026-10-20T13:{40-i:02d}:00Z',
                 'created','2026-10-20T13:32:00Z','2026-10-20T13:32:00Z'))
    return db


class Fake:
    def __init__(self, output, groups='{"groups":[]}'):
        self.output=output
        self.groups=groups
        self.responses=self
        self.calls=[]
    def create(self, **request):
        self.calls.append(request)
        if request['text']['format']['type']=='json_schema':
            return SimpleNamespace(status='completed',output_text=self.groups)
        return SimpleNamespace(status='completed',output_text=self.output)


def test_one_malformed_company_only_keeps_one_article_pending(tmp_path):
    db=fixture(tmp_path)
    ai=Fake('1|amazon com inc|An Amazon business announcement.\n'
            '2|broken name|\n'
            '3|NONE\n'
            '4|Unknown Ventures Ltd.|A stated business involvement.')
    stats=process_ai(db,ai,now=NOW)
    rows={r['news_id']:dict(r) for r in db.execute('SELECT news_id,status,companies FROM news')}
    assert (stats['batches'],stats['partial_batches'],stats['errors'])==(1,1,1)
    assert (stats['failed_article_decisions'],stats['main'])==(1,3)
    assert (rows['n1']['status'],rows['n1']['companies'])==('connected','AMAZON COM INC')
    assert (rows['n2']['status'],rows['n2']['companies'])==('created','')
    assert (rows['n3']['status'],rows['n3']['companies'])==('skipped','')
    assert (rows['n4']['status'],rows['n4']['companies'])==('extracted','Unknown Ventures Ltd.')
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==1
    assert 'AMAZON COM INC|AMZN' in json.loads(ai.calls[1]['input'])['company_catalog']
    assert 'MANDATORY' in ai.calls[1]['instructions']
    assert 'not the article wording' in ai.calls[1]['instructions']
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    db.close()


def test_missing_article_stays_created_and_retries(tmp_path):
    db=fixture(tmp_path,3)
    first=process_ai(db,Fake('1|NONE\n3|NONE'),now=NOW)
    assert first['failed_article_decisions']==1
    assert db.execute("SELECT status FROM news WHERE news_id='n2'").fetchone()[0]=='created'
    second=process_ai(db,Fake('1|AMAZON COM INC|Amazon is named.'),now=NOW)
    # Only one created article remains. It may be deferred because batch-only
    # mode requires two articles; it must NEVER become skipped by assumption.
    assert second['deferred']==1
    assert db.execute("SELECT status FROM news WHERE news_id='n2'").fetchone()[0]=='created'
    db.close()


def test_contradiction_quarantines_own_article_but_not_neighbors(tmp_path):
    db=fixture(tmp_path,3)
    ai=Fake('1|NONE\n1|AMAZON COM INC|Conflict.\n'
            '2|AMAZON COM INC|Amazon operates the named site.\n3|NONE')
    result=process_ai(db,ai,now=NOW)
    assert result['failed_article_decisions']==1
    assert db.execute("SELECT status FROM news WHERE news_id='n1'").fetchone()[0]=='created'
    assert db.execute("SELECT status FROM news WHERE news_id='n2'").fetchone()[0]=='connected'
    assert db.execute("SELECT status FROM news WHERE news_id='n3'").fetchone()[0]=='skipped'
    db.close()


def test_invalid_dedup_group_does_not_block_other_articles(tmp_path):
    db=fixture(tmp_path,4)
    ai=Fake('1|NONE\n2|NONE',groups='{"groups":[[1,1,2]]}')
    stats=process_ai(db,ai,now=NOW)
    assert stats['failed_article_decisions']==2
    assert stats['batches']==1
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='created'").fetchone()[0]==2
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='skipped'").fetchone()[0]==2
    db.close()


def test_no_python_evidence_gate_for_indirect_company(tmp_path):
    db=fixture(tmp_path,2)
    with db:
        db.execute("UPDATE news SET title='Government announces national retail rule',"
                   "summary='The new rules change the retail market for merchants' WHERE news_id='n1'")
    stats=process_ai(db,Fake('1|AMAZON COM INC|The retail rule may affect the company.\n2|NONE'),now=NOW)
    assert stats['relations']==1
    assert db.execute("SELECT status FROM news WHERE news_id='n1'").fetchone()[0]=='connected'
    db.close()


def test_lowercase_response_passes_structural_parser():
    failed={}
    result=parse_lines('1|amazon com inc|Amazon expanded operations.\n'
                       '2|NONE', {1:'n1',2:'n2'},
                       {identity_key('AMAZON COM INC'):'AMAZON COM INC'},
                       failed_articles=failed)
    assert not failed and result['n1'][0][0]=='AMAZON COM INC'


def test_valid_duplicate_group_commits_when_another_group_is_corrupt(tmp_path):
    db=fixture(tmp_path,4)
    ai=Fake('1|AMAZON COM INC|The company is related.\n2|NONE',
            groups='{"groups":[[1,2],[3,3]]}')
    stats=process_ai(db,ai,now=NOW)
    rows={r['news_id']:r['status'] for r in db.execute('SELECT news_id,status FROM news')}
    assert rows == {'n1':'connected','n2':'skipped','n3':'created','n4':'skipped'}
    assert stats['failed_article_decisions']==1
    assert stats['duplicates']==1
    db.close()


def test_fully_unusable_extraction_keeps_every_article_pending(tmp_path):
    db=fixture(tmp_path,2)
    stats=process_ai(db,Fake('1|AMAZON COM INC|\n2|NONE\n2|AMAZON COM INC|contradiction.'),now=NOW)
    assert stats['batches']==0
    assert stats['failed_article_decisions']==2
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='created'").fetchone()[0]==2
    db.close()


def test_none_with_fake_reason_is_not_a_company():
    failed={}
    valid=parse_lines('1|NONE|Not a company\n2|NONE',{1:'n1',2:'n2'},failed_articles=failed)
    assert valid=={'n2':[]}
    assert 'n1' in failed
