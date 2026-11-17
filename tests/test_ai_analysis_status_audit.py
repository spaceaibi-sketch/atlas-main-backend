"""Offline regression checks for current News Phase 2 and SEC Item AI state machine."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from ai.client import analyze_item
from ai.worker import process_pending_ai
from dashboard.company_analytics import company_assessments, refresh_company_aggregates
from news_pipeline.scoring import pending_relations, score_pending
from news_pipeline.scoring_cli import main as score_news_cli
from news_pipeline.store import ensure_schema
from sec_edgar.database import connect

STAMP = '2026-10-20T11:00:00Z'


def db_with_news(tmp_path, relations_per_news=(2,1)):
    path = tmp_path / 'status.sqlite3'
    db = connect(path)
    ensure_schema(db)
    with db:
        db.execute("INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES ('0000001001','Sample Corp','SMPL',?,?)", (STAMP,STAMP))
        company_id = db.execute("SELECT id FROM companies WHERE cik='0000001001'").fetchone()[0]
        for index, total in enumerate(relations_per_news):
            nid = 'n'+str(index)
            db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,summary,source,
                published_utc,companies,status,created_at,updated_at)
                VALUES (?,?,?, ?,?,?,?,'Sample Feed',?,'Sample Corp','connected',?,?)''',
                (nid,'Sample Corp signs a contract','https://test.local/'+nid,
                 nid,nid,nid,'Sample Corp signs a contract for equipment.',STAMP,STAMP,STAMP))
            for j in range(total):
                # Distinct entity keys for testing how one News status depends on
                # multiple candidate assessments. Real rows normally have one per CIK.
                rid = f'r{index}{j}'
                db.execute('''INSERT INTO news_company_relations(relation_id,news_id,company_id,
                    entity_key,entity_type,entity_name,status,correlation_reason,created_at,updated_at)
                    VALUES (?,?,?,?,?,'Sample Corp','connected','Potential exposure',?,?)''',
                    (rid,nid,company_id,f'CIK:0000001001:{j}','company',STAMP,STAMP))
        db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,
            source,published_utc,companies,status,created_at,updated_at)
            VALUES ('none','Not a linked company','https://test.local/none','none','none','none',
            'Sample Feed',?,'Unknown Brand','extracted',?,?)''',(STAMP,STAMP,STAMP))
    return db,path,company_id


def good_score(relation_id):
    return dict(relation_id=relation_id, ai_impact=5, ai_confidence=8,
        ai_reason='Possible economic benefit but indirect exposure is not proven.',
        ai_evidence='Sample Corp signs a contract',
        ai_monetary_effect=0,
        ai_monetary_effect_reason='No quantifiable company-specific dollar effect.',
        price_change_1w_pct=0,
        price_change_reason='Neutral speculative scenario due to insufficient quantified evidence.')


class NewsFake:
    def __init__(self, build=None):
        self.responses = self
        self.build = build
        self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        request = json.loads(kwargs['input'])['news_entity_pairs']
        results = [good_score(row['relation_id']) for row in request]
        result = {'results': results}
        if self.build:
            result = self.build(result)
        return SimpleNamespace(status='completed', output_text=json.dumps(result))


def test_news_scoring_selects_new_schema_and_updates_both_statuses(tmp_path):
    db,path,_ = db_with_news(tmp_path)
    assert not any(row[1] == 'batch_status' for row in db.execute('PRAGMA table_info(news)'))
    assert len(pending_relations(db)) == 3
    fake = NewsFake()
    result = score_pending(db,fake,limit=0)
    assert result['errors'] == 0 and result['analysed_relations'] == 3
    assert result['analysed_news'] == 2 and result['remaining'] == 0
    assert db.execute("SELECT status FROM news WHERE news_id='none'").fetchone()[0] == 'extracted'
    assert {r[0] for r in db.execute("SELECT status FROM news WHERE news_id IN ('n0','n1')")} == {'analysed'}
    assert db.execute("SELECT COUNT(*) FROM news_company_relations WHERE status='analysed'").fetchone()[0] == 3
    assert len(fake.calls) == 1 and fake.calls[0]['timeout'] == 300.0
    assert score_pending(db,fake,limit=0)['selected'] == 0
    db.close()
    # Test the actual CLI dry run against the migrated schema, no API key.
    assert score_news_cli(['--db',str(path),'--dry-run']) == 0


def test_news_partial_response_saves_valid_scores_and_retries_missing(tmp_path):
    db,_,cid = db_with_news(tmp_path)
    def corrupt(payload):
        payload['results'][1]['ai_evidence'] = 'Quote not present in article'
        payload['results'].pop()  # one missing result
        return payload
    first = score_pending(db, NewsFake(corrupt), limit=0)
    assert first['analysed_relations'] == 1
    assert first['failed_relations'] == 2 and first['errors'] == 2
    assert first['remaining'] == 2 and first['partial_batches'] == 1
    assert db.execute("SELECT status FROM news WHERE news_id='n0'").fetchone()[0] == 'connected'
    assert db.execute("SELECT status FROM news WHERE news_id='n1'").fetchone()[0] == 'connected'
    assert db.execute("SELECT status FROM news_company_relations WHERE relation_id='r00'").fetchone()[0] == 'analysed'
    # A completed score is visible in aggregate analytics even if the article
    # remains connected while a second relation awaits assessment.
    rows = company_assessments(db,cid,datetime(2026,10,20,12,tzinfo=timezone.utc))
    assert len([r for r in rows if r['source_type']=='news']) == 1
    retry = score_pending(db,NewsFake(),limit=0)
    assert retry['analysed_relations'] == 2 and retry['remaining'] == 0
    assert retry['analysed_news'] == 2
    assert db.execute("SELECT status FROM news WHERE news_id='n0'").fetchone()[0] == 'analysed'
    db.close()


def test_news_singleton_is_not_deferred(tmp_path):
    db,_,_ = db_with_news(tmp_path,(1,))
    fake=NewsFake()
    result=score_pending(db,fake,limit=0,batch_size=20)
    assert result['analysed_relations'] == 1 and result['deferred'] == 0
    assert len(fake.calls) == 1
    db.close()


def test_news_duplicate_ai_result_only_quarantines_affected_relation(tmp_path):
    db,_,_ = db_with_news(tmp_path,(1,1))
    def duplicate(payload):
        payload['results'].append(dict(payload['results'][0]))
        return payload
    first=score_pending(db,NewsFake(duplicate),limit=0)
    assert first['analysed_relations']==1 and first['remaining']==1
    assert first['failed_relations']==1
    second=score_pending(db,NewsFake(),limit=0)
    assert second['analysed_relations']==1 and second['remaining']==0
    db.close()


def test_news_unrecoverable_json_keeps_status_pending(tmp_path):
    db,_,_ = db_with_news(tmp_path,(2,))
    class Garbled:
        responses=None
        def __init__(self): self.responses=self
        def create(self,**kwargs):
            return SimpleNamespace(status='completed',output_text='not json {oops')
    assert score_pending(db,Garbled(),limit=0)['errors']==1
    assert db.execute("SELECT COUNT(*) FROM news_company_relations WHERE status='connected'").fetchone()[0]==2
    assert score_pending(db,NewsFake(),limit=0)['analysed_relations']==2
    db.close()


def db_with_items(tmp_path, bodies=('Sample Corp signs a contract.','Sample Corp pays $20,000.')):
    db=connect(tmp_path/'items.sqlite3')
    with db:
        db.execute("INSERT INTO companies(cik,name,created_at,updated_at) VALUES ('0000012345','Sample Corp',?,?)",(STAMP,STAMP))
        cid=db.execute("SELECT id FROM companies WHERE cik='0000012345'").fetchone()[0]
        form=db.execute('''INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
            VALUES (?,'8-K','Current filing',?,?,'parsed',?,?)''',
            (cid,STAMP,'https://test.local/filing',STAMP,STAMP)).lastrowid
        for ix,body in enumerate(bodies,1):
            db.execute('''INSERT INTO items(form_id,item_type,item_occurrence,item_name,item_text,status,created_at,updated_at)
                VALUES (?,'1.01',?,'Contract',?,'processed',?,?)''',(form,ix,body,STAMP,STAMP))
    return db


def good_item(row):
    return dict(ai_impact=4,ai_confidence=8,ai_reason='Disclosed operating agreement',
        ai_evidence=row['item_text'],ai_monetary_effect=0,
        ai_monetary_effect_reason='Direct dollar impact not quantifiable',
        price_change_1w_pct=0,
        price_change_reason='Neutral qualitative price scenario, not measured.')


class ItemFake:
    def __init__(self, fail_call=None):
        self.responses=self
        self.calls=0
        self.fail_call=fail_call
    def create(self,**kwargs):
        self.calls+=1
        if self.calls == self.fail_call:
            raise ValueError('Simulated malformed item response')
        row=json.loads(kwargs['input'])
        return SimpleNamespace(status='completed',output_text=json.dumps(good_item(row)))


def test_sec_failure_is_per_item_and_retry_is_explicit(tmp_path):
    db=db_with_items(tmp_path)
    fake=ItemFake(fail_call=1)
    first=process_pending_ai(db,fake,limit=0,batch_size=1)
    assert first['errors']==1 and first['analysed']==1
    assert [r[0] for r in db.execute('SELECT status FROM items ORDER BY item_occurrence')] == ['error','analysed']
    no_retry=process_pending_ai(db,ItemFake(),limit=0)
    assert no_retry['selected']==0
    second=process_pending_ai(db,ItemFake(),limit=0,retry_errors=True)
    assert second['analysed']==1 and second['remaining']==0
    assert db.execute("SELECT COUNT(*) FROM items WHERE status='analysed'").fetchone()[0]==2
    db.close()


def test_sec_reanalysis_failure_preserves_previous_assessment(tmp_path):
    db=db_with_items(tmp_path,('Sample Corp signs a contract.',))
    assert process_pending_ai(db,ItemFake(),limit=0)['analysed']==1
    with db:
        db.execute("UPDATE items SET price_change_reason='Pre-existing old scenario reason', price_change_1w_pct=0")
    before=db.execute('SELECT ai_reason,ai_impact FROM items').fetchone()[:]
    failed=process_pending_ai(db,ItemFake(fail_call=1),limit=0,reanalyse_zero_prices=True)
    assert failed['errors']==1
    assert db.execute('SELECT status,ai_reason,ai_impact FROM items').fetchone()[:] == ('analysed', *before)
    success=process_pending_ai(db,ItemFake(),limit=0,reanalyse_zero_prices=True)
    assert success['analysed']==1 and success['reanalysed_zero_prices']==1
    assert success['remaining']==0
    db.close()


def test_sec_does_not_assess_items_in_unparsed_forms(tmp_path):
    db=db_with_items(tmp_path)
    with db: db.execute("UPDATE forms SET status='error'")
    assert process_pending_ai(db,ItemFake(),limit=0,dry_run=True)['eligible']==0
    db.close()


def test_scoring_adapter_disables_sdk_retries(tmp_path):
    db=db_with_items(tmp_path,('Sample Corp signs a contract.',))
    row=db.execute('''SELECT i.*,f.form_type,c.cik,c.name company_name,f.index_url
        FROM items i JOIN forms f ON f.id=i.form_id JOIN companies c ON c.id=f.company_id''').fetchone()
    class FakeScoped:
        def __init__(self):
            self.options=[]
            self.responses=self
        def with_options(self,**kwargs):
            self.options.append(kwargs)
            return self
        def create(self,**kwargs):
            record=json.loads(kwargs['input'])
            return SimpleNamespace(status='completed',output_text=json.dumps(good_item(record)))
    fake=FakeScoped()
    assert analyze_item(fake,row)['ai_impact']==4
    assert fake.options==[{'timeout':300.0,'max_retries':0}]
    db.close()


def test_production_status_auditor_read_only(tmp_path):
    from scripts.audit_ai_status import audit, main
    db,path,_=db_with_news(tmp_path,(1,1))
    count_before=db.execute('SELECT COUNT(*) FROM news').fetchone()[0]
    result=audit(db)
    assert result['status_anomaly_total']==0
    assert result['ai_queues']['news_relations_ready_for_ai']==2
    assert main(['--db',str(path)])==0
    assert db.execute('SELECT COUNT(*) FROM news').fetchone()[0]==count_before
    with db: db.execute("UPDATE news SET status='analysed' WHERE news_id='n0'")
    result=audit(db)
    assert result['status_anomalies']['news_analysed_without_completed_relations']==1
    assert main(['--db',str(path)])==2
    db.close()
