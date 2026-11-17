"""Executable formula checks and non-destructive database migration regression."""
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from sec_edgar.database import connect, upsert_company
from news_pipeline.store import ensure_schema
from dashboard.company_analytics import refresh_company_aggregates, company_assessments
from dashboard.explorer_queries import companies

NOW=datetime(2026,10,17,21,tzinfo=timezone.utc)

def test_seven_day_weighted_company_summary_and_evidence(tmp_path):
    db=connect(tmp_path/'atlas.sqlite3');ensure_schema(db)
    with db:
        c=upsert_company(db,'212','Test Co')
        other=upsert_company(db,'213','Unrelated Co')
        form=db.execute('''INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,
             status,created_at,updated_at) VALUES(?,'8-K','Event',?,'https://sec.example/test',
             'parsed',?,?)''',(c,'2026-10-17T21:00:00Z','2026-10-17T21:00:00Z','2026-10-17T21:00:00Z')).lastrowid
        db.execute('''INSERT INTO items(form_id,item_type,item_name,item_text,status,
           ai_impact,ai_confidence,ai_monetary_effect,ai_reason,ai_evidence,created_at,updated_at)
           VALUES(?,'8.01','Event','Verifiable text','analysed',8,10,1000,'Expanded product line',
            'Production started','2026-10-17T21:00:00Z','2026-10-17T21:00:00Z')''',(form,))
        db.execute('''INSERT INTO items(form_id,item_type,item_name,item_text,status,
            ai_impact,ai_confidence,ai_monetary_effect,created_at,updated_at)
            VALUES(?,'2.02','Other','Unassessed text','processed',10,10,9000000,?,?)''',
            (form,'2026-10-17T21:00:00Z','2026-10-17T21:00:00Z'))
        for nid,dt,status,batch in (
          ('recent','2026-10-14T09:00:00Z','analysed','main'),
          ('duplicate','2026-10-14T09:00:00Z','analysed','duplicate'),
          ('old','2026-10-08T21:00:00Z','analysed','main'),
          ('future','2026-10-18T21:00:00Z','analysed','main')):
            db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,
               source,published_utc,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'key','Publisher',?,?,?,?)''',
               (nid,nid,'https://example/'+nid,'key'+nid,nid,dt,('skipped' if batch=='duplicate' else status),'2026-10-17T20:00:00Z','2026-10-17T20:00:00Z'))
            db.execute('''INSERT INTO news_company_relations(relation_id,news_id,company_id,
              entity_key,entity_type,entity_name,status,ai_impact,ai_confidence,
              ai_monetary_effect,correlation_reason,ai_reason,ai_evidence,created_at,updated_at)
              VALUES(?,?,?,'CIK:0000000212','company','Test Co','analysed',-4,5,-200,
                'Contract in affected sector','Contract cancellation','Contract canceled',?,?)''',
              (nid,nid,c,'2026-10-17T20:00:00Z','2026-10-17T20:00:00Z'))
    refresh_company_aggregates(db,NOW)
    r=dict(db.execute('''SELECT acumm_impact,acumm_monetary,accum_confdent
                         FROM companies WHERE id=?''',(c,)).fetchone())
    assert r=={'acumm_impact':7.0,'acumm_monetary':900.0,'accum_confdent':8.3333}
    components=company_assessments(db,c,NOW)
    assert len(components)==2 and {v['source_type'] for v in components}=={'news','item'}
    assert next(v for v in components if v['source_type']=='news')['correlation_reason']=='Contract in affected sector'
    assert next(v for v in components if v['source_type']=='item')['evidence']=='Production started'
    assert dict(db.execute('SELECT acumm_impact,acumm_monetary,accum_confdent FROM companies WHERE id=?',(other,)).fetchone()) == {
        'acumm_impact':0.0,'acumm_monetary':0.0,'accum_confdent':0.0}
    sorted_rows, count, _=companies(db,order='impact')
    assert count==2 and sorted_rows[0]['cik']=='0000000212'
    # Once the two eligible records age out, the cached columns are cleared.
    refresh_company_aggregates(db,datetime(2026,10,26,21,tzinfo=timezone.utc))
    assert db.execute('SELECT acumm_impact FROM companies WHERE id=?',(c,)).fetchone()[0]==0
    db.close()


def test_additive_company_migration_preserves_records_and_backup(tmp_path):
    dbfile=tmp_path/'old.sqlite3'
    with sqlite3.connect(dbfile) as raw:
        raw.execute('''CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT,cik TEXT NOT NULL UNIQUE,
             name TEXT NOT NULL,ticker TEXT,exchange TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        raw.execute("INSERT INTO companies(cik,name,created_at,updated_at) VALUES('0000000099','Legacy', 't','t')")
    db=connect(dbfile)
    assert {'acumm_impact','acumm_monetary','accum_confdent'} <= {r[1] for r in db.execute('PRAGMA table_info(companies)')}
    assert db.execute('SELECT name FROM companies').fetchone()[0]=='Legacy'
    db.close()
    assert (tmp_path/'old.sqlite3.before_company_analytics_v12_2.bak').exists()
    old=sqlite3.connect(tmp_path/'old.sqlite3.before_company_analytics_v12_2.bak')
    assert 'acumm_impact' not in {r[1] for r in old.execute('PRAGMA table_info(companies)')}
    old.close()
    db=connect(dbfile);db.close()
    assert len(list(tmp_path.glob('*.before_company_analytics_v12_2.bak')))==1


def test_exact_status_labels_only():
    from dashboard.data_quality import _GROUPS
    for row in _GROUPS:
        assert all(code==label for code,label in row[-1])


def test_sec_item_analytics_without_news_pipeline(tmp_path):
    db=connect(tmp_path/'sec_only.db')
    with db:
        cid=upsert_company(db,'0000000998','Item-only company')
        fid=db.execute("""INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
              VALUES(?,'8-K','Event','2026-10-17T21:00:00Z','https://sec.example/new','parsed','t','t')""",(cid,)).lastrowid
        db.execute("""INSERT INTO items(form_id,item_type,item_name,item_text,status,ai_impact,ai_confidence,
              ai_monetary_effect,created_at,updated_at)
              VALUES(?,'8.01','Event','Substance','analysed',6,5,200,'t','t')""",(fid,))
    refresh_company_aggregates(db,NOW)
    x=db.execute('SELECT acumm_impact,acumm_monetary,accum_confdent FROM companies WHERE id=?',(cid,)).fetchone()
    assert tuple(x)==(3.0,200.0,5.0)
    assert len(company_assessments(db,cid,NOW))==1
    db.close()
