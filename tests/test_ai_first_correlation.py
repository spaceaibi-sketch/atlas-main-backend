"""v10+ AI-first correlation regression tests. No network or real AI costs."""
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory

import pytest
from sec_edgar.database import connect, upsert_company
from sec_edgar.settings import database_path
from news_pipeline.store import ensure_schema, add_story
from news_pipeline.feeds import Story
from news_pipeline.company_extraction import parse_lines
from news_pipeline.matching import resolve_extracted_companies
from news_pipeline.cli import process_ai

NOW = datetime(2026,10,17,14,tzinfo=timezone.utc)


@pytest.fixture
def db():
    with TemporaryDirectory() as folder:
        c = connect(Path(folder)/'db.sqlite3')
        ensure_schema(c)
        with c:
            upsert_company(c,'0000010101','NEWS CORP','NWS')
            upsert_company(c,'0000010102','PAID INC','PAYD')
            upsert_company(c,'0000010103','Example Infrastructure Inc.','EXI')
            upsert_company(c,'0000010104','Apple Inc.','AAPL')
        yield c
        c.close()


def add(db, title, summary=''):
    return add_story(db,Story(title,'https://example.org/news/'+str(abs(hash(title))),summary,
                              (NOW-timedelta(minutes=10)).isoformat(), 'Fixture','US','Economy'))[1]


class MockAI:
    def __init__(self, responder):
        self.responses = self
        self.calls = []
        self.responder = responder

    def create(self, **kwargs):
        self.calls.append(kwargs)
        data = json.loads(kwargs['input'])
        if kwargs['text']['format']['type'] == 'json_schema':
            return SimpleNamespace(status='completed',output_text=json.dumps({'groups': []}))
        return SimpleNamespace(status='completed',output_text=self.responder(data['news']))


def test_parser_requires_every_id_and_real_reason():
    expected = {1:'id-a',2:'id-b'}
    assert parse_lines('1|NONE\n2|Apple Inc.|Apple has a named agreement.',expected) == {
        'id-a':[],'id-b':[('Apple Inc.','Apple has a named agreement.')]}
    assert parse_lines('1|news|Generic word\n2|NONE',expected) == {
        'id-a':[('news','Generic word')], 'id-b':[]}  # no capitalization gate
    for bad in ['1|NONE', '1|Apple Inc.', '1|NONE\n1|Apple Inc.|Both',
                '1|NONE\n2|Apple Inc.|',
                '1|NONE\n9|NONE']:
        with pytest.raises(ValueError):
            parse_lines(bad,expected)


def test_resolve_only_complete_names_and_no_generic_words(db):
    extracted={'n':[('news','irrelevant'),('paid','irrelevant'),
                    ('Example Infrastructure Inc.','Article documents an active contract.'),
                    ('Unknown Contractor Ltd.','Not in SEC catalog')]}
    matches=resolve_extracted_companies(db,extracted)['n']
    assert [(m[2],m[3]) for m in matches] == [
        ('Example Infrastructure Inc.','Article documents an active contract.')]


def test_ai_first_stores_pipe_delimited_company_names_and_relation_reason(db):
    id_a=add(db,'Embraer S.A. (EMBJ) Stock Price, News, Quote & History',
             'generic quote page')
    id_b=add(db,'Saudi government revises funding for national infrastructure',
             'Example Infrastructure Inc. holds a documented ongoing government contract on the named project.')
    def response(rows):
        output=[]
        for row in rows:
            if 'Embraer' in row['title']:
                output.append(f"{row['id']}|NONE")
            else:
                output.append(f"{row['id']}|Example Infrastructure Inc.|The reported funding decision changes the scope of its documented active project contract.")
        return '\n'.join(output)
    api=MockAI(response)
    result=process_ai(db,api,now=NOW)
    assert result['errors']==0 and result['main']==2 and result['relations']==1
    assert len(api.calls)==2  # grouped then extracted, never per matched company
    news_rows={row['news_id']:row for row in db.execute('SELECT * FROM news')}
    assert news_rows[id_a]['companies']==''
    assert news_rows[id_b]['companies']=='Example Infrastructure Inc.'
    rel=db.execute('SELECT * FROM news_company_relations').fetchone()
    assert rel['correlation_reason'].startswith('The reported funding decision')
    assert rel['entity_name']=='Example Infrastructure Inc.'
    assert rel['status']=='connected'
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='skipped'").fetchone()[0]==1
    assert news_rows[id_a]['status']=='skipped'
    assert news_rows[id_b]['status']=='connected'


def test_bad_second_ai_response_only_retries_missing_article(db):
    add(db,'A company reports a new investment')
    add(db,'Another distinct event reports a new investment')
    api=MockAI(lambda news: '1|NONE')  # Missing second News ID
    result=process_ai(db,api,now=NOW)
    assert result['errors']==1 and result['main']==1 and result['failed_article_decisions']==1
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='created'").fetchone()[0]==1
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='skipped'").fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==0


def test_none_never_creates_paid_inc_relation(db):
    add(db,'Nearly All Volume on FIFA-Linked Prediction Market Is Circular Trading',
        "ADI Predictstreet, backed by Abu Dhabi's royal family, paid close to $150 million for advertising rights during the World Cup.")
    add(db,'Unrelated earnings story')
    api=MockAI(lambda news: '\n'.join(f"{n['id']}|NONE" for n in news))
    result=process_ai(db,api,now=NOW)
    assert result['relations']==0
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==0


def test_existing_v10_db_adds_both_columns(tmp_path):
    c=connect(tmp_path/'legacy.sqlite3')
    ensure_schema(c)
    # Verify schema idempotence and database is not migrated to a different path.
    ensure_schema(c)
    news_cols={r[1] for r in c.execute('PRAGMA table_info(news)')}
    relation_cols={r[1] for r in c.execute('PRAGMA table_info(news_company_relations)')}
    assert 'companies' in news_cols and 'batch_status' not in news_cols
    assert not {'country','industry','government_name','scope','main_news_id','batch_reviewed_at','batch_reason','company_reasons_json'} & news_cols
    assert 'correlation_reason' in relation_cols
    c.close()


def test_database_default_is_one_level_above_backend():
    assert database_path().parent.name=='data'
    assert database_path().parent.parent == Path(__file__).resolve().parents[2]


def test_ai_is_responsible_for_company_selection_without_post_ai_evidence_gate(db):
    add(db,'Embraer S.A. Stock Price, News, Quote & History', 'Stock market updates')
    add(db,'Nearly all prediction markets were paid for in ads', 'Paid advertising in sports')
    api=MockAI(lambda rows: '\n'.join(
        f"{r['id']}|NEWS CORP|The page contains the word news." if 'Embraer' in r['title']
        else f"{r['id']}|PAID INC|The article contains the word paid."
        for r in rows))
    result=process_ai(db,api,now=NOW)
    # The deterministic linker does not second-guess an explicit AI identity.
    # Phase 1 prompt must reject incidental common-word matches; Phase 2
    # assesses the relationship, which is not yet verified as meaningful.
    assert result['extracted_companies']==2
    assert result['resolved_companies']==2
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==2
    assert all(row[0]=='NEWS CORP' or row[0]=='PAID INC' for row in db.execute('SELECT companies FROM news'))


def test_verified_indirect_exposure_without_company_name_in_article(db):
    add(db,'Saudi Arabia modifies budget for project infrastructure',
        'The government announced modifications to the rail project timeline.')
    add(db,'Another independent weather event')
    api=MockAI(lambda rows: '\n'.join(
        f"{r['id']}|Example Infrastructure Inc.|The amended rail project affects the company's documented current contract."
        if 'Saudi Arabia' in r['title'] else f"{r['id']}|NONE" for r in rows))
    no_evidence=process_ai(db,api,now=NOW)
    assert no_evidence['resolved_companies']==1
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==1
    # Re-run on a fresh separate database so the old main is not submitted twice.
    with TemporaryDirectory() as folder:
        other=connect(Path(folder)/'other.sqlite3')
        ensure_schema(other)
        with other:
            upsert_company(other,'0000010103','Example Infrastructure Inc.','EXI')
        add(other,'Saudi Arabia modifies budget for project infrastructure',
            'The government announced modifications to the rail project timeline.')
        add(other,'Another independent weather event')
        evidence=[{'company':'Example Infrastructure Inc.','country':'Saudi Arabia',
                   'sector':'Rail','business_connection':'Active rail contract',
                   'evidence':'Original dated contract notice','source_url':'https://example.org/official'}]
        passed=process_ai(other,MockAI(api.responder),now=NOW,exposures=evidence)
        assert passed['resolved_companies']==1
        assert other.execute('SELECT correlation_reason FROM news_company_relations').fetchone()[0].startswith('The amended rail')
        other.close()


def test_verified_full_ceo_name_qualifies(db):
    add(db,'CEO Jane Example said the company will acquire new assets',
        'Jane Example disclosed the proposed company acquisition at the conference.')
    add(db,'An unrelated independent report')
    ceos=[{'company':'Apple Inc.','ceo':'Jane Example','evidence':'Fictional regression CEO fixture',
           'source_url':'https://example.org/ceo-profile'}]
    api=MockAI(lambda rows: '\n'.join(
        f"{r['id']}|Apple Inc.|The named CEO disclosed a company acquisition plan."
        if r['title'].startswith('CEO') else f"{r['id']}|NONE" for r in rows))
    result=process_ai(db,api,now=NOW,ceos=ceos)
    assert result['resolved_companies']==1
    assert db.execute('SELECT entity_name FROM news_company_relations').fetchone()[0]=='Apple Inc.'


def test_rebuild_existing_connected_keyword_relation_and_preview(db):
    from news_pipeline.review_existing import legacy_mains,rebuild
    article=add(db,'Nearly All Volume on FIFA-Linked Prediction Market Is Circular Trading',
                'A group paid close to $150 million to advertise.')
    add(db,'Unrelated news to ensure valid dedup batches')
    with db:
        db.execute("UPDATE news SET status='connected' WHERE news_id=?",(article,))
        cid=db.execute("SELECT id FROM companies WHERE name='PAID INC'").fetchone()[0]
        db.execute("""INSERT INTO news_company_relations
            (relation_id,news_id,company_id,entity_key,entity_type,entity_name,
             status,created_at,updated_at)
            VALUES('legacy1',?,?,'CIK:0000010102','company','PAID INC','connected','now','now')""",
            (article,cid))
    assert len(legacy_mains(db,10))==1
    api=MockAI(lambda news: '\n'.join(f"{r['id']}|NONE" for r in news))
    count=rebuild(db,api,limit=10)
    assert count==1
    assert len(api.calls)==1
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==0
    assert db.execute('SELECT status FROM news WHERE news_id=?',(article,)).fetchone()[0]=='extracted'



def test_uncatalogued_ai_company_is_saved_without_unverified_relation(db):
    news_id=add(db,'Government announces a major new tax on energy assets',
                'The country announces a new tax affecting documented energy operations.')
    another=add(db,'Unrelated market event','No affected companies reported.')
    def responder(rows):
        return '\n'.join(
            f"{row['id']}|Uncatalogued Energy PLC|The announcement affects its documented taxable projects."
            if row['title'].startswith('Government') else f"{row['id']}|NONE" for row in rows)
    result=process_ai(db,MockAI(responder),now=NOW)
    assert result['errors']==0 and result['batches']==1
    assert result['resolved_companies']==0 and result['no_company_skipped']==1
    assert db.execute('SELECT companies FROM news WHERE news_id=?',(news_id,)).fetchone()[0]=='Uncatalogued Energy PLC'
    assert db.execute('SELECT status FROM news WHERE news_id=?',(news_id,)).fetchone()[0]=='extracted'
    assert db.execute('SELECT status FROM news WHERE news_id=?',(another,)).fetchone()[0]=='skipped'
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==0


def test_sec_name_ticker_catalog_is_sent_once_per_extraction_batch(db):
    from news_pipeline.company_extraction import load_company_catalog, prepare_extraction, parse_lines
    catalog, identities = load_company_catalog(db)
    assert 'Apple Inc.|AAPL' in catalog.splitlines()
    assert 'Example Infrastructure Inc.|EXI' in catalog.splitlines()
    assert 'NEWS CORP|NWS' in catalog.splitlines()
    rows = [{'news_id':'story-1','title':'Apple Inc. builds facility',
             'summary':'Apple Inc. opens an existing facility.'}]
    payload, lookup = prepare_extraction(rows, company_catalog=catalog)
    decoded = json.loads(payload)
    assert decoded['company_catalog'] == catalog
    assert isinstance(decoded['company_catalog'],str)
    assert decoded['news'][0]['id'] == 1
    assert parse_lines('1|APPLE INC.|The company operates the facility.',lookup,identities) == {
        'story-1':[('Apple Inc.','The company operates the facility.')]}
    assert parse_lines('1|Unlisted Corp.|Supported company involvement in article.',lookup,identities)=={
        'story-1':[('Unlisted Corp.','Supported company involvement in article.')]}

    add(db,'Apple Inc. signs a new project agreement',
        'Apple Inc. signs a documented project agreement.')
    add(db,'Unrelated company event','Nothing relevant here')
    api=MockAI(lambda news: '\n'.join(
        f"{n['id']}|Apple Inc.|The company signs the new project agreement."
        if 'Apple' in n['title'] else f"{n['id']}|NONE" for n in news))
    result=process_ai(db,api,now=NOW)
    assert result['errors']==0
    assert len(api.calls)==2
    request_catalog=json.loads(api.calls[1]['input'])['company_catalog']
    assert request_catalog==catalog
    assert 'industry' not in api.calls[1]['input']


def test_catalog_validates_dangerous_separator_and_missing_ticker(db):
    from news_pipeline.company_extraction import load_company_catalog
    with db:
        db.execute("UPDATE companies SET ticker=NULL WHERE name='Apple Inc.'")
    catalog,_=load_company_catalog(db)
    assert 'Apple Inc.|' in catalog.splitlines()
    with db:
        db.execute("UPDATE companies SET ticker='AA|PL' WHERE name='Apple Inc.'")
    with pytest.raises(ValueError,match='Invalid company'):
        load_company_catalog(db)


def test_large_sec_catalog_is_compact_and_bounded(db):
    from news_pipeline.company_extraction import load_company_catalog, prepare_extraction
    with db:
        db.executemany('INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES(?,?,?,?,?)',
                       [(f'{i+30000:010}',f'Example Issuer {i} Incorporated',f'EX{i}','now','now')
                        for i in range(8000)])
    catalog,_=load_company_catalog(db)
    assert len(catalog.splitlines()) == 8004
    payload,_=prepare_extraction([{'news_id':'1','title':'Example Issuer 5 announces contract'}],
                                  company_catalog=catalog)
    assert len(payload.encode('utf-8')) < 700_000

def test_legacy_verified_relation_fills_missing_old_raw_names(tmp_path):
    path=tmp_path/'verified.sqlite3'
    db=connect(path);ensure_schema(db)
    with db:
        db.execute("ALTER TABLE news ADD COLUMN batch_status TEXT NOT NULL DEFAULT 'pending'")
        upsert_company(db,'0000080001','Test Energy Inc.','TENG')
        cid=db.execute("SELECT id FROM companies WHERE cik='0000080001'").fetchone()[0]
        db.execute("ALTER TABLE news ADD COLUMN company_reasons_json TEXT NOT NULL DEFAULT '[]'")
        db.execute("INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,status,batch_status,created_at,updated_at) "
                   "VALUES('old-news','old','old-url','old-key','tk','sk','src','analysed','main','now','now')")
        db.execute("INSERT INTO news_company_relations (relation_id,news_id,company_id,entity_key,entity_type,entity_name,created_at,updated_at) "
                   "VALUES('r1','old-news',?,'CIK:0000080001','company','Test Energy Inc.','now','now')",(cid,))
    ensure_schema(db)
    assert db.execute("SELECT companies FROM news WHERE news_id='old-news'").fetchone()[0]=='Test Energy Inc.'
    assert db.execute("SELECT COUNT(*) FROM news_company_relations WHERE news_id='old-news'").fetchone()[0]==1
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    db.close()

def test_migration_backs_up_and_preserves_legacy_company_names_and_dedup(tmp_path):
    dbfile=tmp_path/'before.sqlite3'
    c=connect(dbfile)
    ensure_schema(c)
    with c:
        c.execute("ALTER TABLE news ADD COLUMN batch_status TEXT NOT NULL DEFAULT 'pending'")
        c.execute("ALTER TABLE news ADD COLUMN company_reasons_json TEXT NOT NULL DEFAULT '[]'")
        c.execute("ALTER TABLE news ADD COLUMN main_news_id TEXT REFERENCES news(news_id)")
        c.execute("ALTER TABLE news ADD COLUMN batch_reviewed_at TEXT NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE news ADD COLUMN batch_reason TEXT NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE news ADD COLUMN country TEXT NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE news ADD COLUMN industry TEXT NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE news ADD COLUMN government_name TEXT NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE news ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        c.execute("INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,status,batch_status,created_at,updated_at,company_reasons_json) "
                  "VALUES('main','Main story','u-main','uk-main','tk-main','sk-main','example','connected','main','now','now',?)",
                  (json.dumps(['Apple Inc.|Existing reason', 'Example Infrastructure Inc.|Contract']),))
        c.execute("INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,status,batch_status,created_at,updated_at,main_news_id) "
                  "VALUES('dup','Duplicate','u-dup','uk-dup','tk-dup','sk-dup','example','skipped','duplicate','now','now','main')")
    ensure_schema(c)
    cols={r[1] for r in c.execute('PRAGMA table_info(news)')}
    assert 'companies' in cols and 'company_reasons_json' not in cols and 'main_news_id' not in cols
    assert c.execute("SELECT companies FROM news WHERE news_id='main'").fetchone()[0]=='Apple Inc.|Example Infrastructure Inc.'
    assert c.execute("SELECT status FROM news WHERE news_id='dup'").fetchone()[0]=='skipped'
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='news_duplicate_links'").fetchone()
    assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert dbfile.with_name('before.sqlite3.before_news_single_status_v13.bak').exists()
    backup=sqlite3.connect(dbfile.with_name('before.sqlite3.before_news_single_status_v13.bak'))
    assert 'company_reasons_json' in {r[1] for r in backup.execute('PRAGMA table_info(news)')}
    backup.close()
    ensure_schema(c)
    assert c.execute("SELECT COUNT(*) FROM news WHERE status='skipped'").fetchone()[0]==1
    c.close()


def test_batch_connection_report_counts_distinct_news_and_companies(db,caplog):
    """Two companies on one story must count as two links, one connected article."""
    add(db,'Apple Inc. and Example Infrastructure Inc. announce partnership',
        'Apple Inc. and Example Infrastructure Inc. formally sign a joint investment agreement.')
    add(db,'Another unrelated earnings article','No companies named here.')
    def response(rows):
        output=[]
        for row in rows:
            if row['title'].startswith('Apple Inc.'):
                output.append(f"{row['id']}|Apple Inc.|Apple Inc. signed the joint investment agreement.")
                output.append(f"{row['id']}|Example Infrastructure Inc.|Example Infrastructure Inc. signed the joint investment agreement.")
            else:
                output.append(f"{row['id']}|NONE")
        return '\n'.join(output)
    captured=[]
    api=MockAI(response)
    with caplog.at_level('INFO',logger='news_pipeline'):
        result=process_ai(db,api,now=NOW,
            request_recorder=lambda number,stage,request: captured.append((number,stage,request)))
    assert result['batches']==1
    assert result['ai_news_with_companies']==1
    assert result['extracted_companies']==2
    assert result['resolved_companies']==2
    assert result['connected_news']==1
    assert result['connected_relations']==2
    assert result['relations']==2
    assert 'AI CONNECTIONS [1] ai_news_with_companies=1 extracted_companies=2 resolved_companies=2 connected_news=1 connected_relations=2' in caplog.text
    assert len(captured)==2
    assert [stage for _,stage,_ in captured]==['event_grouping','company_extraction']
    for (_,_,request),api_call in zip(captured,api.calls):
        # Timeout is transport-level configuration, not part of the recorded AI prompt.
        assert {key: val for key, val in api_call.items() if key != 'timeout'} == request
        assert api_call.get('timeout') == 300.0  # Lightweight test client fallback.
        assert request['instructions'] and request['input']
        assert request['store'] is False
    assert db.execute("SELECT COUNT(*) FROM news_company_relations WHERE status='connected'").fetchone()[0]==2


def test_failed_extraction_keeps_only_bad_article_pending(db,caplog):
    add(db,'First unrelated major report')
    add(db,'Second unrelated major report')
    api=MockAI(lambda news: '1|NONE')  # Missing article 2 must not lose article 1.
    with caplog.at_level('WARNING',logger='news_pipeline'):
        result=process_ai(db,api,now=NOW)
    assert result['errors']==1
    assert result['batches']==1
    assert result['connected_news']==0
    assert result['connected_relations']==0
    assert 'partial:' in caplog.text
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==0
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='created'").fetchone()[0]==1


def test_duplicate_decision_is_only_skipped_and_has_no_main_link(db):
    from news_pipeline.store import save_batch_groups
    first = add(db, 'First publication about the same event')
    second = add(db, 'Second publication about the same event')
    with db:
        assert save_batch_groups(db,[{'news_id':first},{'news_id':second}],
                                 [first],{second:first}) == (1,1)
    main = db.execute('SELECT status FROM news WHERE news_id=?',(first,)).fetchone()
    dup = db.execute('SELECT status,companies FROM news WHERE news_id=?',(second,)).fetchone()
    assert main['status']=='created'
    assert dup['status']=='skipped' and dup['companies']==''
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='news_duplicate_links'").fetchone()


def test_legacy_link_table_is_dropped_with_a_snapshot(tmp_path):
    dbfile=tmp_path/'old_links.sqlite3'
    c=connect(dbfile)
    ensure_schema(c)
    with c:
        c.execute("ALTER TABLE news ADD COLUMN batch_status TEXT NOT NULL DEFAULT 'pending'")
        c.execute("INSERT INTO news (news_id,title,url,url_key,title_key,story_key,source,status,batch_status,created_at,updated_at) "
                  "VALUES('main','main','a','a','a','a','source','created','main','now','now')")
        c.execute("INSERT INTO news (news_id,title,url,url_key,title_key,story_key,source,status,batch_status,created_at,updated_at,companies) "
                  "VALUES('duplicate','dup','b','b','b','b','source','connected','duplicate','now','now','Stale candidate')")
        c.execute('CREATE TABLE news_duplicate_links (news_id TEXT PRIMARY KEY,main_news_id TEXT)')
        c.execute("INSERT INTO news_duplicate_links VALUES('duplicate','main')")
    ensure_schema(c)
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='news_duplicate_links'").fetchone()
    row=c.execute("SELECT status,companies FROM news WHERE news_id='duplicate'").fetchone()
    assert row['status']=='skipped' and row['companies']==''
    backup=dbfile.with_name(dbfile.name+'.before_news_unlink_v12.bak')
    assert backup.exists()
    with sqlite3.connect(backup) as saved:
        assert saved.execute('SELECT COUNT(*) FROM news_duplicate_links').fetchone()[0]==1
    c.close()


def test_no_ceo_registry_or_article_company_mention_needed_for_candidate(db):
    add(db, 'Government updates electric-vehicle charging rules',
        'The policy changes charging infrastructure requirements.')
    add(db, 'An unrelated agricultural report', 'Another independent event.')
    api = MockAI(lambda rows: '\n'.join(
        f"{r['id']}|Apple Inc.|AI proposes an indirect impact for Phase 2 to evaluate."
        if 'charging' in r['title'] else f"{r['id']}|NONE" for r in rows))
    outcome = process_ai(db, api, now=NOW)
    assert outcome['errors'] == 0
    assert outcome['resolved_companies'] == 1
    assert outcome['relations'] == 1
    assert db.execute("SELECT COUNT(*) FROM news WHERE status='connected'").fetchone()[0] == 1
