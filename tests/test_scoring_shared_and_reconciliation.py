"""Regression: shared News/SEC batching and Android/main score cache repair."""
from datetime import datetime, timezone

from shared.ai_scoring import batch_score_schema, validate_batch_results
from dashboard.mobile_summary import refresh_company_summaries
from scripts.reconcile_ai_aggregates import audit, main as reconcile_main
from news_pipeline.scoring import score_pending
from tests.test_ai_analysis_status_audit import db_with_news, NewsFake


def test_batch_schema_and_partial_errors_shared():
    expected = {'r1': {'id':'r1'}, 'r2': {'id':'r2'}, 'r3':{'id':'r3'}}
    payload={'results':[
        {'relation_id':'r1','v':5}, {'relation_id':'r2','v':3},
        {'relation_id':'r2','v':3}, {'relation_id':'oops','v':4},
    ]}
    ok, bad, unexpected=validate_batch_results(
        payload,expected,id_field='relation_id',id_type=str,
        validate_one=lambda score,row: None if score['v'] <= 10 else (_ for _ in ()).throw(ValueError('invalid')))
    assert list(ok)==['r1']
    assert set(bad)=={'r2','r3'} and unexpected==1
    assert batch_score_schema(id_field='relation_id',id_type='string')['properties']['results']['items']['properties']['relation_id']=={'type':'string'}
    assert batch_score_schema(id_field='item_id',id_type='integer')['properties']['results']['items']['properties']['item_id']=={'type':'integer'}
    # Integer IDs cannot silently coerce bool or strings.
    valid,failed,unknown=validate_batch_results({'results':[{'item_id':True},{'item_id':'2'},{'item_id':2}]},
        {2:object()},id_field='item_id',id_type=int,validate_one=lambda *_:None)
    assert list(valid)==[2] and not failed and unknown==2


def test_new_scoring_updates_android_cache_and_reconcile_reports(tmp_path):
    db,path,cid=db_with_news(tmp_path,(1,1))
    # An old cache with no scores should be flagged after scoring state changes.
    refresh_company_summaries(db)
    before=db.execute('SELECT aggregate_score FROM summary WHERE company_id=?',(cid,)).fetchone()[0]
    assert before is None
    fake=NewsFake()
    response=score_pending(db,fake,limit=0)
    assert response['analysed_relations']==2 and response['postprocess_errors']==0
    stored=db.execute('SELECT aggregate_score FROM summary WHERE company_id=?',(cid,)).fetchone()[0]
    assert stored==10
    now=datetime(2026,10,20,12,tzinfo=timezone.utc)
    initial=audit(db,now=now)
    assert initial['mobile_mismatched']==0
    with db:
        db.execute('UPDATE summary SET aggregate_score=NULL WHERE company_id=?',(cid,))
    mismatch=audit(db,now=now)
    assert mismatch['mobile_mismatched']==1
    db.close()
    # A no-write dry run must leave the stale value intact.
    assert reconcile_main(['--db',str(path),'--report-dir',str(tmp_path/'reports')])==0
    assert (tmp_path/'reports'/'score_reconciliation.csv').exists()
    assert reconcile_main(['--db',str(path),'--apply','--report-dir',str(tmp_path/'reports')])==0
    assert list(tmp_path.glob('*.before_score_reconcile_*.bak'))
    from sec_edgar.database import connect
    conn=connect(path)
    assert conn.execute('SELECT aggregate_score FROM summary WHERE company_id=?',(cid,)).fetchone()[0]==10
    conn.close()


def test_sec_batch_scoring_refreshes_android_impact_cache(tmp_path):
    from tests.test_sec_ai_batch_requests import prepared_db, BatchClient
    from ai.worker import process_pending_ai
    db=prepared_db(tmp_path,3)
    cid=db.execute('SELECT id FROM companies').fetchone()[0]
    refresh_company_summaries(db)
    assert db.execute('SELECT aggregate_score FROM summary WHERE company_id=?',(cid,)).fetchone()[0] is None
    result=process_pending_ai(db,BatchClient(),limit=0,batch_size=2)
    assert result['analysed']==3 and result['postprocess_errors']==0
    assert db.execute('SELECT aggregate_score FROM summary WHERE company_id=?',(cid,)).fetchone()[0]==9
    assert audit(db)['mobile_mismatched']==0
    db.close()
