"""Regression tests for durable Massive fetching and a single SQLite writer."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, current_thread
import sqlite3

from sec_edgar.database import connect
from price_pipeline.cli import run
from price_pipeline.durable_writer import MassivePriceSpool
from price_pipeline.massive_writer import _is_busy

NOW = datetime(2026, 10, 21, 19, tzinfo=timezone.utc)
BAR = {'t':'2026-10-21T13:30:00Z', 'o':100, 'h':103, 'l':99, 'c':101}


def setup_db(tmp_path, names=('AAPL',)):
    path = tmp_path / 'prices.sqlite3'
    with connect(path) as db:
        for i, name in enumerate(names):
            db.execute('INSERT INTO companies(cik,name,ticker,created_at,updated_at) '
                       'VALUES(?,?,?,?,?)', (str(i+1),name,name,
                                             '2026-10-21T00:00:00Z',
                                             '2026-10-21T00:00:00Z'))
    return path


def run_fake(path, factory, *, now=NOW, threads=1):
    return run(['--db',str(path),'--source','massive','--last','1',
                '--no-gap-repair','--threads',str(threads)],
               now=now,massive_factory=factory)


def test_parallel_fetches_one_writer_thread_and_durable_ack(tmp_path, monkeypatch, capsys):
    path = setup_db(tmp_path, ('AAPL','MSFT','META','NVDA'))
    monkeypatch.setenv('MASSIVE_API_KEY','TEST')
    barrier = Barrier(4, timeout=10)
    fetched_threads = set()
    class Provider:
        source,feed='massive','sip'
        def __init__(self, _key): pass
        def bars(self, *_):
            fetched_threads.add(current_thread().name)
            barrier.wait()
            return [dict(BAR)]
    import price_pipeline.massive_writer as module
    actual = module.collect_company
    write_threads = set()
    def recorded(*args,**kw):
        write_threads.add(current_thread().name)
        return actual(*args,**kw)
    monkeypatch.setattr(module,'collect_company',recorded)
    assert run_fake(path,Provider,threads=4)==0
    assert len(fetched_threads)==4
    assert write_threads=={'massive-db-writer_0'}
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0] == 4
    assert MassivePriceSpool(path).pending_ids()==[]
    output=capsys.readouterr().out
    assert '[PRICE QUEUE] single_writer=1 fetch_threads=4' in output
    assert 'pending_durable=0' in output


def test_sqlite_lock_is_retried_and_batch_verified_before_delete(tmp_path, monkeypatch):
    path = setup_db(tmp_path)
    monkeypatch.setenv('MASSIVE_API_KEY','TEST')
    monkeypatch.setenv('ATLAS_PRICE_LOCK_RETRY_SECONDS','0')
    class Provider:
        source,feed='massive','sip'
        def __init__(self,_key): pass
        def bars(self,*_): return [dict(BAR)]
    import price_pipeline.massive_writer as module
    actual = module.collect_company
    calls=[]
    def locked_once(*a,**kw):
        calls.append(1)
        if len(calls)==1:
            raise sqlite3.OperationalError('database is locked')
        return actual(*a,**kw)
    monkeypatch.setattr(module,'collect_company',locked_once)
    assert run_fake(path,Provider)==0
    assert len(calls)==2
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0]==1
    assert MassivePriceSpool(path).pending_ids()==[]


def test_failed_write_remains_durable_then_replay_without_refetch(tmp_path, monkeypatch, capsys):
    path=setup_db(tmp_path)
    monkeypatch.setenv('MASSIVE_API_KEY','TEST')
    monkeypatch.setenv('ATLAS_PRICE_LOCK_RETRY_ATTEMPTS','1')
    requests=[]
    class Provider:
        source,feed='massive','sip'
        def __init__(self,_key): pass
        def bars(self,symbol,start,end):
            requests.append((symbol,start,end))
            return [dict(BAR)]
    import price_pipeline.massive_writer as module
    actual=module.collect_company
    def always_locked(*a,**kw): raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(module,'collect_company',always_locked)
    assert run_fake(path,Provider)==1
    spool=MassivePriceSpool(path)
    pending=spool.pending_ids()
    assert len(pending)==1
    assert spool.load(pending[0])['state']=='fetched'
    assert spool.load(pending[0])['bars']==[BAR]
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0]==0
    monkeypatch.setattr(module,'collect_company',actual)
    # The older 1-day lookback must still be inserted as originally requested.
    assert run_fake(path,Provider,now=NOW+timedelta(days=3))==0
    assert len(requests)==2  # original + new; recovery uses the persisted bars
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0]==1
    assert spool.pending_ids()==[]
    out=capsys.readouterr().out
    assert 'recovered=1 planned=1' in out
    assert 'pending_durable=0' in out


def test_failed_fetch_preserves_original_period_and_retries(tmp_path, monkeypatch):
    path=setup_db(tmp_path)
    monkeypatch.setenv('MASSIVE_API_KEY','TEST')
    attempts=[]
    class Provider:
        source,feed='massive','sip'
        def __init__(self,_key): pass
        def bars(self,symbol,start,end):
            attempts.append((start,end))
            if len(attempts)==1: raise RuntimeError('provider unavailable')
            return [dict(BAR)]
    assert run_fake(path,Provider)==1
    spool=MassivePriceSpool(path)
    pending=spool.pending_ids()
    assert len(pending)==1
    assert spool.load(pending[0])['state']=='planned'
    assert run_fake(path,Provider,now=NOW+timedelta(days=3))==0
    assert attempts[1]==attempts[0]
    assert spool.pending_ids()==[]
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0]==1


def test_busy_error_classifier():
    assert _is_busy(sqlite3.OperationalError('database is locked'))
    assert _is_busy(sqlite3.OperationalError('database busy'))
    assert not _is_busy(sqlite3.OperationalError('no such table'))


def test_gap_repair_fetch_also_uses_download_worker_and_durable_buffer(tmp_path, monkeypatch, capsys):
    path=setup_db(tmp_path)
    monkeypatch.setenv('MASSIVE_API_KEY','TEST')
    calls=[]
    class Provider:
        source,feed='massive','sip'
        def __init__(self,_key): pass
        def bars(self,symbol,start,end):
            calls.append((symbol,start,end,current_thread().name))
            if start.hour == 13 and start.minute == 45:
                return [{**BAR,'t':'2026-10-21T13:45:00Z'}]
            return [dict(BAR),{**BAR,'t':'2026-10-21T14:00:00Z'}]
    assert run(['--db',str(path),'--source','massive','--last','1',
                '--threads','2'],now=NOW,massive_factory=Provider) == 0
    assert len(calls)==2
    assert all(t[3].startswith('massive-fetch') for t in calls)
    assert calls[1][1].hour == 13 and calls[1][1].minute == 45
    with connect(path) as db:
        assert [r[0] for r in db.execute('SELECT bar_time_utc FROM price_bars ORDER BY bar_time_utc')] == [
            '2026-10-21T13:30:00Z','2026-10-21T13:45:00Z','2026-10-21T14:00:00Z']
    assert MassivePriceSpool(path).pending_ids()==[]
    out=capsys.readouterr().out
    assert 'queued durable gap repair' in out
    assert 'bars_saved=3' in out


def test_gap_repair_fetch_error_persists_and_recovers(tmp_path, monkeypatch):
    path=setup_db(tmp_path)
    monkeypatch.setenv('MASSIVE_API_KEY','TEST')
    calls=[]
    class Provider:
        source,feed='massive','sip'
        def __init__(self,_key): pass
        def bars(self,symbol,start,end):
            calls.append((start,end))
            if start.hour==13 and start.minute==45:
                if len(calls)==2:
                    raise RuntimeError('repair provider unavailable')
                return [{**BAR,'t':'2026-10-21T13:45:00Z'}]
            if start.date()==NOW.date():
                return []
            return [dict(BAR),{**BAR,'t':'2026-10-21T14:00:00Z'}]
    assert run(['--db',str(path),'--source','massive','--last','1',
                '--threads','1'],now=NOW,massive_factory=Provider) == 1
    spool=MassivePriceSpool(path)
    pending=spool.pending_ids()
    assert len(pending)==1 and spool.load(pending[0])['kind']=='gap_repair'
    assert spool.load(pending[0])['state']=='planned'
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0]==2
    assert run(['--db',str(path),'--source','massive','--last','1',
                '--threads','1'],now=NOW+timedelta(days=1),massive_factory=Provider) == 0
    assert spool.pending_ids()==[]
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0]==3


def test_corrupted_spool_is_never_silently_discarded(tmp_path):
    from price_pipeline.durable_writer import SpoolError
    path = setup_db(tmp_path)
    spool = MassivePriceSpool(path)
    from price_pipeline.collector import requested_period
    row={'id':1,'ticker':'AAPL','last_activity':''}
    job=spool.plan(row,requested_period(NOW,1),NOW)
    filename=spool._path(job['id'])
    old=filename.read_text()
    filename.write_text(old.replace('AAPL','MSFT'))
    import pytest
    with pytest.raises(SpoolError, match='checksum mismatch'):
        spool.load(job['id'])
    assert filename.exists()


def test_real_external_sqlite_writer_lock_retains_and_replays_prices(tmp_path, monkeypatch):
    path=setup_db(tmp_path)
    monkeypatch.setenv('MASSIVE_API_KEY','TEST')
    monkeypatch.setenv('ATLAS_PRICE_LOCK_RETRY_ATTEMPTS','1')
    monkeypatch.setenv('ATLAS_SQLITE_BUSY_TIMEOUT_MS','1000')
    class Provider:
        source,feed='massive','sip'
        def __init__(self,_key): pass
        def bars(self,*_): return [dict(BAR)]
    blocker=sqlite3.connect(path)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        blocker.execute("UPDATE companies SET name='locked' WHERE id=1")
        assert run_fake(path, Provider)==1
        spool=MassivePriceSpool(path)
        pending=spool.pending_ids()
        assert len(pending)==1
        assert spool.load(pending[0])['state']=='fetched'
    finally:
        blocker.rollback()
        blocker.close()
    assert run_fake(path, Provider)==0
    assert spool.pending_ids()==[]
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0]==1
