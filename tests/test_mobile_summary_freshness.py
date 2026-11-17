"""Canonical five-table regression for mobile summary freshness and repair."""
from __future__ import annotations
from pathlib import Path
import sqlite3
from datetime import datetime,timezone
from sec_edgar.database import connect
from dashboard.mobile_summary import refresh_company_summaries
from scripts.repair_mobile_summary import audit, run

T='2026-10-25T12:00:00Z'


def _seed(db):
    with db:
        db.execute("INSERT INTO companies(id,cik,name,created_at,updated_at) VALUES(1,'0000000001','Test',?,?)",(T,T))
        db.executemany("""INSERT INTO raw_items(id,source_type,source_key,title,event_at,received_at,
            status,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'{}',?,?)""",[
            (1,'news','n1','New story','2026-10-25T17:00:00+00:00',T,'connected',T,T),
            (2,'filing','f1','Form',T,T,'created',T,T),
            (3,'item','i1','8K Item',T,T,'analysed',T,T),
            (4,'news','n2','Skipped',T,T,'skipped',T,T),
            (5,'news','n3','Unlinked',T,T,'created',T,T),
        ])
        db.executemany("""INSERT INTO relationships(id,company_id,source_id,entity_key,
            entity_type,entity_name,ai_impact,created_at,updated_at)
            VALUES(?,1,? ,?,'company','Test',?,?,?)""",[
            (1,1,'news:n1',2,T,T),
            (2,2,'filing:f1',None,T,T),
            (3,3,'item:i1',-4,T,T),
            (4,4,'news:n2',7,T,T),
        ])
        db.execute("INSERT INTO summary(company_id,refreshed_at,last_price) VALUES(1,?,10)",(T,))
        db.execute("UPDATE summary SET latest_published='2026-10-20T00:00:00Z',aggregate_score=99 WHERE company_id=1")


def test_backed_up_repair_refreshes_news_and_impact(tmp_path):
    path=tmp_path/'fresh.sqlite3'
    db=connect(path);_seed(db)
    with db:
        db.execute("INSERT INTO price_bars(company_id,bar_time_utc,open,high,low,close,volume,feed,ingested_at) VALUES(1,?,?,?,?,?,?,?,?)",(T,10,11,9,10,100,'yfinance',T))
    prior=db.execute('SELECT last_price FROM summary WHERE company_id=1').fetchone()[0]
    db.close()
    with sqlite3.connect(path) as ro:
        before=audit(ro)
    assert before['impact_mismatch']==1
    assert before['news_date_mismatch']==1
    assert run(path,False)==0
    assert run(path,True)==0
    assert len(list(tmp_path.glob('*.bak')))==1
    with sqlite3.connect(path) as after:
        assert all(v==0 for k,v in audit(after).items() if k!='companies')
        row=after.execute('SELECT latest_published,latest_item_filed,aggregate_score,related_news_count,last_price FROM summary WHERE company_id=1').fetchone()
        assert row[0]=='2026-10-25T17:00:00.000Z'
        assert row[1]=='2026-10-25T12:00:00.000Z'
        assert row[2]==-2
        assert row[3]==1
        assert row[4]==prior


def test_later_ingestion_only_updates_after_refresh(tmp_path):
    db=connect(tmp_path/'live.sqlite3');_seed(db)
    refresh_company_summaries(db,refresh_prices=False)
    assert db.execute('SELECT aggregate_score FROM summary WHERE company_id=1').fetchone()[0]==-2
    with db:
        db.execute("UPDATE relationships SET ai_impact=5,updated_at=? WHERE id=1",('2026-10-25T19:00:00Z',))
        db.execute("UPDATE raw_items SET event_at=?,updated_at=? WHERE id=1",('2026-10-25T18:00:00Z','2026-10-25T19:00:00Z'))
    assert audit(db)['impact_mismatch']==1
    refresh_company_summaries(db,refresh_prices=False)
    assert audit(db)['impact_mismatch']==0
    row=db.execute('SELECT aggregate_score,latest_published FROM summary WHERE company_id=1').fetchone()
    assert tuple(row)==(1,'2026-10-25T18:00:00.000Z')
    db.close()


def test_extraction_wrappers_refresh_after_partial_failure_and_skip_dry_run(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    for wrapper in ('extract_news.sh','extract_forms_items.sh','extract_recent_filings.sh'):
        folder=tmp_path/wrapper.replace('.sh','');(folder/'scripts').mkdir(parents=True)
        (folder/wrapper).write_bytes((root/wrapper).read_bytes())
        stub=folder/'scripts/run_python.sh'
        stub.write_text('#!/usr/bin/env bash\n'
                        'echo "$*" >> "$ATLAS_TEST_CALLS"\n'
                        'if [[ "${ATLAS_TEST_FAIL:-0}" == 1 && "$*" != *"dashboard.mobile_summary"* ]];'
                        'then exit 7; fi\n')
        calls=folder/'calls.log'
        env={**os.environ,'ATLAS_TEST_CALLS':str(calls)}
        result=subprocess.run(['bash',str(folder/wrapper)],env=env,capture_output=True)
        assert result.returncode==0,(wrapper,result.stderr)
        assert calls.read_text().count('dashboard.mobile_summary --events-only')==1
        calls.unlink()
        result=subprocess.run(['bash',str(folder/wrapper)],env={**env,'ATLAS_TEST_FAIL':'1'},capture_output=True)
        assert result.returncode==7
        assert calls.read_text().count('dashboard.mobile_summary --events-only')==1
        calls.unlink()
        result=subprocess.run(['bash',str(folder/wrapper),'--dry-run'],env=env,capture_output=True)
        assert result.returncode==0
        assert 'dashboard.mobile_summary' not in calls.read_text()


def test_mobile_summary_latest_compares_utc_instants_across_offsets(tmp_path):
    """Stored mobile summary uses UTC chronology, not lexical clock strings."""
    db=connect(tmp_path/'offset_comparison.sqlite3')
    _seed(db)
    with db:
        # 20:00+03 is 17:00Z; 17:30Z is newer despite sorting lower as text.
        db.execute("UPDATE raw_items SET event_at=? WHERE id=1", ('2026-10-25T20:00:00+03:00',))
        db.execute("""INSERT INTO raw_items(id,source_type,source_key,title,event_at,received_at,
            status,metadata_json,created_at,updated_at) VALUES (6,'news','n6','Later news',
            ?,?,'connected','{}',?,?)""", ('2026-10-25T17:30:00Z',T,T,T))
        db.execute("""INSERT INTO relationships(id,company_id,source_id,entity_key,
            entity_type,entity_name,ai_impact,created_at,updated_at)
            VALUES(5,1,6,'news:n6','company','Test',1,?,?)""", (T,T))
        db.execute("UPDATE raw_items SET event_at=? WHERE id=3", ('2026-10-25T23:00:00+07:00',))
    refresh_company_summaries(db,refresh_prices=False)
    row=db.execute("""SELECT latest_published,latest_item_filed,aggregate_score,
        latest_event_at,last_assessed_at FROM summary WHERE company_id=1""").fetchone()
    assert row[0]=='2026-10-25T17:30:00.000Z'
    assert row[1]=='2026-10-25T16:00:00.000Z'
    assert row[2]==-1
    assert row[3]=='2026-10-25T17:30:00.000Z'
    assert row[4].endswith('Z')
    assert audit(db)['news_date_mismatch']==0
    assert audit(db)['item_date_mismatch']==0
    db.close()
