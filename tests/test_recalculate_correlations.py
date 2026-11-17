"""Regression tests for deterministic News historical relinking. No AI/network."""
import json
import sqlite3
from pathlib import Path

from sec_edgar.database import connect, upsert_company
from news_pipeline.store import ensure_schema
from news_pipeline.recalculate_correlations import plan, apply_plan, main


def insert_news(conn, nid, name, title, *, status='extracted', summary=''):
    with conn:
        conn.execute('''INSERT INTO news
            (news_id,title,url,url_key,title_key,story_key,summary,article_text,
             source,published_utc,companies,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (nid,title, f'https://example.test/{nid}',nid,nid,nid,summary,'',
             'Source','2026-10-19T10:00:00Z',name,status,
             '2026-10-19T10:02:00Z','2026-10-19T10:02:00Z'))


def fixture(tmp_path):
    conn=connect(tmp_path/'edgar.sqlite3')
    ensure_schema(conn)
    with conn:
        upsert_company(conn,'0001090012','DEVON ENERGY CORP/DE','DVN')
        upsert_company(conn,'0001874178','Rivian Automotive, Inc. / DE','RIVN')
        upsert_company(conn,'0001000001','Example Holdings Inc.','EX1')
        upsert_company(conn,'0001000002','Example Holdings LLC','EX2')
    return conn


def test_transition_resolves_jurisdiction_and_preserves_unmatched_names(tmp_path):
    db=fixture(tmp_path)
    insert_news(db,'n1','Rivian Automotive, Inc.|Unknown Startup','Rivian Automotive, Inc. reported a new vehicle model.')
    insert_news(db,'n2','DEVON ENERGY CORP','DEVON ENERGY CORP announced its quarterly earnings.')
    insert_news(db,'n3','Example Holdings','Example Holdings enters the market.')
    insert_news(db,'n4','Unknown Firm','Unknown Firm announced investments.')
    insert_news(db,'n5','Rivian Automotive, Inc.','Rivian Automotive announced a recall.',status='skipped')
    insert_news(db,'n6','Rivian Automotive, Inc.','Unrelated event contains no company names.')
    before=db.total_changes
    details, stats=plan(db)
    assert db.total_changes==before
    outcomes={(x['news_id'],x['extracted_name']):x['outcome'] for x in details}
    assert outcomes[('n1','Rivian Automotive, Inc.')]=='would_insert'
    assert outcomes[('n2','DEVON ENERGY CORP')]=='would_insert'
    assert outcomes[('n3','Example Holdings')]=='ambiguous'
    assert outcomes[('n1','Unknown Startup')]=='unmatched'
    assert outcomes[('n4','Unknown Firm')]=='unmatched'
    assert outcomes[('n6','Rivian Automotive, Inc.')]=='would_insert'  # indirect candidate; Phase 2 assesses it
    assert 'n5' not in [r['news_id'] for r in details]
    assert stats['eligible_new_links']==3
    assert apply_plan(db,details)==3
    assert dict(db.execute('SELECT news_id,status FROM news WHERE news_id IN ("n1","n2","n3")')) == {'n1':'connected','n2':'connected','n3':'extracted'}
    assert db.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==3
    assert db.execute("SELECT companies FROM news WHERE news_id='n1'").fetchone()[0]=='Rivian Automotive, Inc.|Unknown Startup'
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    again, stats2=plan(db)
    assert stats2['eligible_new_links']==0
    assert apply_plan(db,again)==0
    db.close()


def test_transition_does_not_modify_existing_ai_assessment(tmp_path):
    db=fixture(tmp_path)
    cid=db.execute("SELECT id FROM companies WHERE ticker='RIVN'").fetchone()[0]
    insert_news(db,'scored','Rivian Automotive, Inc.','Rivian Automotive, Inc. makes a statement.',status='analysed')
    with db:
        db.execute('''INSERT INTO news_company_relations
            (relation_id,news_id,company_id,entity_key,entity_type,entity_name,
             status,ai_impact,ai_confidence,ai_reason,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'analysed',7,8.5,'Saved model assessment',?,?)''',
            ('rel1','scored',cid,'CIK:0001874178','company','Rivian Automotive, Inc. / DE',
             '2026-10-19T10:02:00Z','2026-10-19T10:02:00Z'))
    before=tuple(db.execute("SELECT ai_impact,ai_confidence,ai_reason,updated_at FROM news_company_relations WHERE news_id='scored'").fetchone())
    details,summary=plan(db)
    assert summary['eligible_new_links']==0
    assert details[0]['outcome']=='already_linked'
    assert apply_plan(db,details)==0
    after=tuple(db.execute("SELECT ai_impact,ai_confidence,ai_reason,updated_at FROM news_company_relations WHERE news_id='scored'").fetchone())
    assert after==before
    assert db.execute("SELECT status FROM news WHERE news_id='scored'").fetchone()[0]=='analysed'
    db.close()


def test_cli_dry_run_and_apply_write_reports_and_backup(tmp_path):
    db=fixture(tmp_path)
    insert_news(db,'cli','DEVON ENERGY CORP','DEVON ENERGY CORP releases guidance.')
    db.commit();db.close()
    path=tmp_path/'edgar.sqlite3'
    reports=tmp_path/'reports'
    assert main(['--db',str(path),'--report-dir',str(reports)])==0
    assert list(reports.glob('*.csv'))
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==0
    assert main(['--apply','--db',str(path),'--report-dir',str(reports)])==0
    assert list(tmp_path.glob('*.before_news_recorrelation_*.bak'))
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==1
    assert main(['--apply','--db',str(path),'--report-dir',str(reports)])==0
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]==1
    reports_json=sorted(reports.glob('*.json'))
    assert any(json.loads(r.read_text())['summary']['mode']=='applied' for r in reports_json)


def test_new_candidate_on_analysed_news_is_visible_to_phase2(tmp_path):
    from news_pipeline.scoring import pending_relations
    db=fixture(tmp_path)
    insert_news(db,'scored_news','Rivian Automotive, Inc.|DEVON ENERGY CORP',
                'Government policy may affect manufacturing and oil extraction.',
                status='analysed')
    rivian=db.execute("SELECT id FROM companies WHERE ticker='RIVN'").fetchone()[0]
    with db:
        db.execute('''INSERT INTO news_company_relations
            (relation_id,news_id,company_id,entity_key,entity_type,entity_name,
             status,ai_impact,ai_confidence,ai_reason,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'analysed',3,9,'Stored Phase 2 result',?,?)''',
            ('already_scored','scored_news',rivian,'CIK:0001874178','company',
             'Rivian Automotive, Inc. / DE', '2026-10-19T10:02:00Z',
             '2026-10-19T10:02:00Z'))
    details,stats=plan(db)
    assert stats['eligible_new_links']==1
    assert apply_plan(db,details)==1
    assert db.execute("SELECT status FROM news WHERE news_id='scored_news'").fetchone()[0]=='connected'
    assert db.execute("SELECT status,ai_impact,ai_confidence FROM news_company_relations WHERE relation_id='already_scored'").fetchone()[:]==('analysed',3,9)
    pending=pending_relations(db)
    assert len(pending)==1
    assert pending[0]['entity_name']=='DEVON ENERGY CORP/DE'
    db.close()
