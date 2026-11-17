"""Integration of writer-owned mobile impact materialization and price ingestion."""
from datetime import datetime, timezone

from dashboard.mobile_summary import SCHEMA, refresh_company_summaries
from news_pipeline.store import ensure_schema
from price_pipeline.storage import ingest_bars
from scripts.migrate_mobile_impact_summary import migrate, validate, FIELDS
from sec_edgar.database import connect

STAMP = '2026-10-19T12:00:00Z'


def _seed(db):
    with db:
        db.execute("INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES('0000000010','A','AAA',?,?)", (STAMP,STAMP))
        db.execute("INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES('0000000011','B','BBB',?,?)", (STAMP,STAMP))
        db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,
                     published_utc,status,created_at,updated_at)
                     VALUES('n1','News A','https://test/n1','n1','n1','n1','Feed',
                       '2026-10-19T12:00:00Z','analysed',?,?)''', (STAMP,STAMP))
        db.execute('''INSERT INTO news_company_relations(relation_id,news_id,company_id,entity_key,
                     entity_type,entity_name,status,ai_impact,ai_confidence,created_at,updated_at)
                     VALUES('r1','n1',1,'A','company','A','analysed',-7,6,?,?)''', (STAMP,STAMP))
        db.execute('''INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,
                     created_at,updated_at) VALUES(1,'8-K','8-K','2026-10-18T12:00:00Z',
                     'https://test/form',?,?)''',(STAMP,STAMP))
        db.execute('''INSERT INTO items(form_id,item_type,item_name,status,ai_impact,ai_confidence,
                     created_at,updated_at) VALUES(1,'1.01','Agreement','analysed',5,8,?,?)''',(STAMP,STAMP))


def _bar(ts, close):
    return dict(t=ts, o=close,h=close, l=close,c=close,v=100,n=1,vw=close)


def test_migration_backfill_and_transactional_auto_updates(tmp_path):
    db=connect(tmp_path/'writer.sqlite3')
    ensure_schema(db)
    _seed(db)
    # Exercise the actual old production layout, with the new fields absent.
    old=SCHEMA[:SCHEMA.index(' max_news_impact REAL,')] + SCHEMA[SCHEMA.index(' last_price REAL,'):]
    db.executescript(old)
    assert validate(db)
    with db:
        migrate(db, True)
    cols={r[1] for r in db.execute('PRAGMA table_info(summary)')}
    assert set(FIELDS) <= cols
    def summary(cid=1):
        return dict(db.execute('SELECT * FROM summary WHERE company_id=?',(cid,)).fetchone())
    a=summary()
    assert (a['aggregate_score'], a['max_news_impact'], a['max_sec_impact'],
            a['latest_event_impact'], a['latest_event_kind']) == (-2,-7,5,-7,'news')
    # This writes only the main source record, without invoking a UI/API refresh.
    with db:
        db.execute("UPDATE news_company_relations SET ai_impact=9 WHERE relation_id='r1'")
    assert (summary()['aggregate_score'],summary()['max_news_impact']) == (14,9)
    with db:
        db.execute('UPDATE items SET ai_impact=-10 WHERE id=1')
    assert (summary()['max_sec_impact'],summary()['aggregate_score']) == (-10,-1)
    with db:
        db.execute('UPDATE forms SET company_id=2 WHERE id=1')
    assert summary()['max_sec_impact'] is None
    assert summary(2)['max_sec_impact'] == -10
    # Existing backend summary rebuild must preserve and calculate new fields.
    refresh_company_summaries(db)
    assert summary()['max_news_impact'] == 9
    assert summary(2)['max_sec_impact'] == -10
    # A latest-bar price update must update only price metrics, not recalculate
    # or erase unrelated event impacts. Count INSERTs, not summary trigger writes.
    now=datetime(2026,10,20,12,tzinfo=timezone.utc)
    bars=[_bar('2026-10-10T12:00:00Z',100),_bar('2026-10-17T12:00:00Z',105),
          _bar('2026-10-18T12:00:00Z',110)]
    inserted=ingest_bars(db,1,bars,datetime(2026,10,9,tzinfo=timezone.utc),now,'sip',now=now)
    assert inserted == 3
    assert summary()['last_price'] == 110
    assert round(summary()['trend_7d'],6)==10
    assert summary()['max_news_impact'] == 9
    assert ingest_bars(db,1,bars,datetime(2026,10,9,tzinfo=timezone.utc),now,'sip',now=now)==0
    with db:
        db.execute("UPDATE news_company_relations SET status='connected' WHERE relation_id='r1'")
    assert summary()['max_news_impact'] is None
    assert summary()['aggregate_score'] is None
    db.close()


def test_sec_only_python_refresh_no_news_tables(tmp_path):
    db=connect(tmp_path/'sec-only.sqlite3')
    db.execute("INSERT INTO companies(cik,name,created_at,updated_at) VALUES('0000000010','A',?,?)",(STAMP,STAMP))
    db.execute("INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,created_at,updated_at) VALUES (1,'8-K','8-K',?,'https://test/sec',?,?)",(STAMP,STAMP,STAMP))
    db.execute("INSERT INTO items(form_id,item_type,item_name,status,ai_impact,created_at,updated_at) VALUES (1,'1.01','Agreement','analysed',-4,?,?)",(STAMP,STAMP))
    refresh_company_summaries(db)
    result=db.execute('SELECT max_sec_impact,latest_event_impact,max_news_impact FROM summary').fetchone()
    assert tuple(result)==(-4,-4,None)
    db.close()


def test_old_price_feed_migration_preserves_mobile_triggers(tmp_path):
    from price_pipeline.storage import PRICE_SCHEMA, ensure_yfinance_feed
    db = connect(tmp_path/'price-upgrade.sqlite3')
    ensure_schema(db)
    _seed(db)
    db.execute('DROP TABLE price_bars')
    db.executescript(PRICE_SCHEMA.replace("('sip', 'iex', 'yfinance')", "('sip', 'iex')"))
    db.executescript(SCHEMA)
    with db:
        migrate(db, True)
    original={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='price_bars'")}
    assert len(original)==3
    assert ensure_yfinance_feed(db)
    current={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='price_bars'")}
    assert current==original
    now=datetime(2026,10,20,12,tzinfo=timezone.utc)
    assert ingest_bars(db,1,[_bar('2026-10-19T12:00:00Z',12)],datetime(2026,10,18,tzinfo=timezone.utc),now,'yfinance',now=now,source='yfinance')==1
    assert db.execute('SELECT last_price FROM summary WHERE company_id=1').fetchone()[0]==12
    db.close()


def test_refresh_job_skips_redundant_full_mobile_scan_when_triggered(tmp_path, monkeypatch):
    from shared.aggregate_refresh import _impact_summary_is_trigger_maintained, refresh_score_caches
    import shared.aggregate_refresh as refresh_module
    db=connect(tmp_path/'skip-full.sqlite3')
    ensure_schema(db)
    _seed(db)
    db.executescript(SCHEMA)
    assert not _impact_summary_is_trigger_maintained(db)
    with db:
        migrate(db,True)
    assert _impact_summary_is_trigger_maintained(db)
    monkeypatch.setattr(refresh_module,'refresh_company_aggregates', lambda connection: None)
    def unexpected(*args):
        raise AssertionError('redundant full-company scan')
    monkeypatch.setattr(refresh_module,'refresh_company_summaries', unexpected)
    class Logger:
        def error(self, *args):
            raise AssertionError(args)
    assert refresh_score_caches(db,Logger())==0
    with db:
        db.execute('UPDATE news_company_relations SET ai_impact=6 WHERE relation_id=\'r1\'')
    assert db.execute('SELECT max_news_impact FROM summary WHERE company_id=1').fetchone()[0]==6
    db.close()


def test_transition_cli_dry_run_backup_and_apply(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    path=tmp_path/'cli.sqlite3'
    db=connect(path)
    ensure_schema(db)
    _seed(db)
    # Current backend table existed before adding the new fields.
    old=SCHEMA[:SCHEMA.index(' max_news_impact REAL,')] + SCHEMA[SCHEMA.index(' last_price REAL,'):]
    db.executescript(old)
    db.commit()
    db.close()
    script=Path(__file__).resolve().parents[1]/'scripts/migrate_mobile_impact_summary.py'
    def run(mode):
        return subprocess.run([sys.executable,str(script),'--db',str(path),mode],
                              capture_output=True,text=True,check=True)
    assert 'Schema OK' in run('--dry-run').stdout
    assert not list(tmp_path.glob('*.pre-mobile-impact-*.bak'))
    assert 'Migration complete' in run('--apply').stdout
    assert len(list(tmp_path.glob('*.pre-mobile-impact-*.bak')))==1
    with connect(path) as check:
        assert check.execute('SELECT max_news_impact FROM summary WHERE company_id=1').fetchone()[0]==-7
        assert check.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_mobile_summary_schema_check_preserves_callers_open_transaction():
    import sqlite3
    from dashboard.mobile_summary import ensure_summary_schema
    db = sqlite3.connect(':memory:')
    ensure_summary_schema(db)
    db.execute('CREATE TABLE unrelated(value INTEGER)')
    db.commit()
    db.execute('BEGIN')
    db.execute('INSERT INTO unrelated(value) VALUES (7)')
    ensure_summary_schema(db)
    assert db.in_transaction, 'schema check must not commit an unrelated writer transaction'
    db.rollback()
    assert db.execute('SELECT COUNT(*) FROM unrelated').fetchone()[0] == 0
    db.close()
