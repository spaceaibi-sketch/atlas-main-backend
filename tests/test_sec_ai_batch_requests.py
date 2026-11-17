"""SEC Item batching: IDs, independent commits, retry, and stale protection."""
import json
from types import SimpleNamespace

import pytest

from ai.client import analyze_items
from ai.worker import process_pending_ai
from ai.cli import main as sec_cli
from sec_edgar.database import connect

STAMP = '2026-10-20T12:00:00Z'


def prepared_db(tmp_path, item_count=5):
    db = connect(tmp_path / 'sec_batch.sqlite3')
    with db:
        cid = db.execute('''INSERT INTO companies(cik,name,created_at,updated_at)
            VALUES ('0000009999','Test Company',?,?)''', (STAMP, STAMP)).lastrowid
        form_id = db.execute('''INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
            VALUES (?,'8-K','Report',?,'https://test.local/form','parsed',?,?)''',
            (cid, STAMP,STAMP,STAMP)).lastrowid
        for index in range(1, item_count + 1):
            db.execute('''INSERT INTO items(form_id,item_type,item_occurrence,item_name,item_text,status,created_at,updated_at)
                VALUES (?,'1.01',?,'Agreement',?,'processed',?,?)''',
                (form_id,index,f'Test Company signed agreement {index}.',STAMP,STAMP))
    return db


def valid_result(item):
    return dict(item_id=item['item_id'],ai_impact=3,ai_confidence=8,
        ai_reason='Possible benefit, amount not quantified.',
        ai_evidence=item['item_text'],
        ai_monetary_effect=0,
        ai_monetary_effect_reason='0 is a placeholder; net USD effect not quantified.',
        price_change_1w_pct=0,
        price_change_reason='Speculative neutral scenario, not an observed return.')


class BatchClient:
    def __init__(self, transform=None):
        self.responses = self
        self.calls=[]
        self.options=[]
        self.transform=transform
    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['text']['format']['name'] == 'sec_item_batch_analysis':
            items=json.loads(kwargs['input'])['items']
            results=[valid_result(item) for item in items]
            payload={'results':results}
        else:
            item=json.loads(kwargs['input'])
            payload={k:v for k,v in valid_result({'item_id':0,**item}).items() if k!='item_id'}
        if self.transform:
            payload=self.transform(payload,kwargs)
        return SimpleNamespace(status='completed',output_text=json.dumps(payload))


def statuses(db):
    return [x[0] for x in db.execute('SELECT status FROM items ORDER BY item_occurrence')]


def test_batch_scoring_groups_items_and_matches_out_of_order_ids(tmp_path):
    db=prepared_db(tmp_path)
    fake=BatchClient(lambda p, _: {'results':list(reversed(p['results']))} if 'results' in p else p)
    result=process_pending_ai(db,fake,limit=0,batch_size=3)
    assert result['analysed']==5 and result['errors']==0
    assert result['batches']==2 and len(fake.calls)==2
    assert fake.options==[{'timeout':300.0,'max_retries':0}]*2
    assert statuses(db)==['analysed']*5
    assert [x[0] for x in db.execute('SELECT ai_evidence FROM items ORDER BY item_occurrence')] == [
        f'Test Company signed agreement {n}.' for n in range(1,6)]
    assert fake.calls[0]['text']['format']['schema']['properties']['results']['items']['properties']['item_id']=={'type':'integer'}
    db.close()


def test_invalid_evidence_missing_and_duplicate_scores_isolate_each_item(tmp_path):
    db=prepared_db(tmp_path,4)
    def damaged(p,_):
        if 'results' not in p:
            return p
        results=p['results']
        results[1]['ai_evidence']='fabricated quote'
        results.append(dict(results[2]))
        results.pop(3)  # Remove the fourth result, keep duplicated third
        return p
    fake=BatchClient(damaged)
    result=process_pending_ai(db,fake,limit=0,batch_size=4)
    # The bad quote is retried as a single Item and recovered; missing and
    # duplicated IDs remain isolated failures.
    assert result['analysed']==2 and result['errors']==2
    assert result['partial_batches']==1
    assert result['evidence_retries']==1 and result['evidence_recovered']==1
    assert statuses(db)==['analysed','analysed','error','error']
    assert db.execute("SELECT ai_evidence FROM items WHERE item_occurrence=1").fetchone()[0] == 'Test Company signed agreement 1.'
    retry=process_pending_ai(db,BatchClient(),limit=0,batch_size=4,retry_errors=True)
    assert retry['analysed']==2 and statuses(db)==['analysed']*4
    db.close()


def test_unknown_id_is_ignored_and_original_result_still_saved(tmp_path):
    db=prepared_db(tmp_path,2)
    def unexpected(payload,kwargs):
        payload['results'].append({**payload['results'][0], 'item_id':999999})
        return payload
    result=process_pending_ai(db,BatchClient(unexpected),limit=0,batch_size=2)
    assert result['analysed']==2 and result['unexpected_scores']==1
    db.close()


def test_completely_corrupted_batch_response_only_marks_selected_items(tmp_path):
    db=prepared_db(tmp_path,3)
    class Broken(BatchClient):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(status='completed',output_text='{not json')
    result=process_pending_ai(db,Broken(),limit=0,batch_size=2)
    assert result['errors']==3 and statuses(db)==['error']*3
    assert process_pending_ai(db,BatchClient(),limit=0,batch_size=2,retry_errors=True)['analysed']==3
    db.close()


def test_batch_success_and_failure_guard_changed_filing(tmp_path):
    db=prepared_db(tmp_path,2)
    class Changed(BatchClient):
        def create(self, **kwargs):
            with db:
                db.execute("UPDATE forms SET status='extracted'")
            return super().create(**kwargs)
    result=process_pending_ai(db,Changed(),limit=0,batch_size=2)
    assert result['analysed']==0 and result['skipped_stale']==2
    assert statuses(db)==['processed']*2
    db.close()


def test_batch_reanalysis_one_invalid_preserves_old_scores(tmp_path):
    db=prepared_db(tmp_path,2)
    assert process_pending_ai(db,BatchClient(),limit=0,batch_size=2)['analysed']==2
    with db:
        db.execute("UPDATE items SET price_change_reason='Old scenario',price_change_1w_pct=0")
    before=list(db.execute('SELECT ai_impact,ai_reason FROM items ORDER BY item_occurrence'))
    def one_bad(payload,_):
        if 'results' not in payload:
            return payload
        payload['results'][0]['ai_evidence']='invented'
        return payload
    stats=process_pending_ai(db,BatchClient(one_bad),limit=0,batch_size=2,reanalyse_zero_prices=True)
    assert stats['analysed']==2 and stats['errors']==0
    assert stats['evidence_retries']==1 and stats['evidence_recovered']==1
    assert statuses(db)==['analysed']*2
    assert list(db.execute('SELECT ai_impact,ai_reason FROM items ORDER BY item_occurrence')) == before
    assert not db.execute("SELECT errors FROM items WHERE item_occurrence=1").fetchone()[0]
    db.close()


def test_empty_item_excluded_from_batch_request(tmp_path):
    db=prepared_db(tmp_path,3)
    with db: db.execute("UPDATE items SET item_text='' WHERE item_occurrence=2")
    fake=BatchClient()
    result=process_pending_ai(db,fake,limit=0,batch_size=3)
    assert result['analysed']==2 and result['rejected']==1
    assert statuses(db)==['analysed','rejected','analysed']
    assert len(json.loads(fake.calls[0]['input'])['items'])==2
    db.close()


def test_batch_size_limits_and_dry_run(tmp_path):
    db=prepared_db(tmp_path,2)
    with pytest.raises(ValueError,match='batch-size'):
        process_pending_ai(db,None,batch_size=0,dry_run=True)
    with pytest.raises(ValueError,match='batch-size'):
        process_pending_ai(db,None,batch_size=26,dry_run=True)
    assert process_pending_ai(db,None,batch_size=3,dry_run=True)['selected']==2
    db.close()


def test_authentication_failure_does_not_flip_pending_rows_to_errors(tmp_path):
    db=prepared_db(tmp_path,4)
    AuthenticationError = type('AuthenticationError',(Exception,),{})
    class Unavailable(BatchClient):
        def create(self,**kwargs):
            raise AuthenticationError('Bad key')
    stats=process_pending_ai(db,Unavailable(),limit=0,batch_size=3)
    assert stats['errors']==1 and stats['analysed']==0
    assert statuses(db)==['processed']*4
    db.close()


def test_corrupt_batch_envelope_recovers_by_single_item_requests(tmp_path):
    db=prepared_db(tmp_path,3)
    class GarbledBatch(BatchClient):
        def create(self, **kwargs):
            if kwargs['text']['format']['name'] == 'sec_item_batch_analysis':
                self.calls.append(kwargs)
                return SimpleNamespace(status='completed',output_text='{incomplete')
            return super().create(**kwargs)
    fake=GarbledBatch()
    stats=process_pending_ai(db,fake,limit=0,batch_size=3)
    assert stats['analysed']==3 and stats['errors']==0
    assert stats['batch_fallbacks']==1 and len(fake.calls)==4
    assert statuses(db)==['analysed']*3
    db.close()


def test_corrupt_batch_fallback_saves_good_singletons_and_isolates_bad_one(tmp_path):
    db=prepared_db(tmp_path,3)
    class GarbledBatch(BatchClient):
        def create(self, **kwargs):
            if kwargs['text']['format']['name'] == 'sec_item_batch_analysis':
                return SimpleNamespace(status='completed',output_text='not-json')
            return super().create(**kwargs)
    def damage(payload, request):
        if 'results' not in payload and 'agreement 2.' in request['input']:
            payload['ai_evidence']='invented quote'
        return payload
    fake=GarbledBatch(damage)
    stats=process_pending_ai(db,fake,limit=0,batch_size=3)
    assert stats['analysed']==2 and stats['errors']==1
    assert stats['batch_fallbacks']==1
    assert statuses(db)==['analysed','error','analysed']
    db.close()
