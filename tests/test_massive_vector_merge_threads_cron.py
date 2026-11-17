"""Offline regressions: vectorized merge, bounded parallel fetch, calendar repair."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, current_thread
import time

from price_pipeline.storage import ingest_bars, missing_regular_slots
from price_pipeline.collector import collect_company, requested_period
from price_pipeline.cli import run
from price_pipeline.massive import SharedRateLimiter
from sec_edgar.database import connect
from shared.config_cron import FIELDS

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 19, tzinfo=UTC)

def bar(t, close=101, **overrides):
    data = {'t': t, 'o': 100, 'h': 103, 'l': 99, 'c': close,
            'v': 100, 'n': 5, 'vw': 101.25}
    return dict(data, **overrides)


def make_db(tmp_path, names=('AAPL',)):
    db = connect(tmp_path / 'merge.sqlite3')
    with db:
        for i, name in enumerate(names):
            db.execute("""INSERT INTO companies(cik,name,ticker,created_at,updated_at)
                VALUES (?,?,?,?,?)""", (str(i + 1), name, name,
                    '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z'))
    return db


def test_vectorized_merge_validity_dedup_nonoverwrite_and_mid_gap(tmp_path):
    db = make_db(tmp_path)
    start = datetime(2026,10,21,tzinfo=UTC)
    a, b, c = '2026-10-21T13:30:00Z', '2026-10-21T13:45:00Z', '2026-10-21T14:00:00Z'
    try:
        bad = [bar('2026-10-21T13:37:00Z'), bar('2026-10-21T13:15:00', 100),
               bar('2026-10-21T14:15:00Z', 100, v=1.5),
               bar('2026-10-21T14:30:00Z', 100, v=-1),
               bar('2026-10-21T14:45:00Z', 105),
               bar('2026-10-21T15:00:00Z', 100, n=-1),
               bar('2026-10-21T15:15:00Z', 100, vw=float('nan'))]
        # Price-only Massive accepts these valid OHLC rows despite invalid
        # volume/trade metadata; timestamps and invalid OHLC remain rejected.
        assert ingest_bars(db,1,[bar(c), *bad, bar(a), bar(a, close=102)],start,NOW,'sip',now=NOW,source='massive') == 6
        assert [r[0] for r in db.execute('SELECT bar_time_utc FROM price_bars ORDER BY bar_time_utc')] == [a,c,'2026-10-21T14:15:00Z','2026-10-21T14:30:00Z','2026-10-21T15:00:00Z','2026-10-21T15:15:00Z']
        assert [t.strftime('%Y-%m-%dT%H:%M:%SZ') for t in missing_regular_slots(db, 1, start, NOW)] == [b,'2026-10-21T14:45:00Z']
        assert ingest_bars(db, 1, [bar(b),bar('2026-10-21T14:45:00Z'),bar(a,close=102)],start,NOW,'sip',now=NOW,source='massive') == 2
        assert list(db.execute('SELECT close FROM price_bars WHERE bar_time_utc=?',(a,)))[0][0] == 101
        assert missing_regular_slots(db, 1, start, NOW) == []
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0] == 8
        assert ingest_bars(db,1,[],start,NOW,'sip',now=NOW,source='massive') == 0
    finally:
        db.close()


def test_gap_repair_inserts_only_middle_missing_bar(tmp_path):
    db = make_db(tmp_path)
    row = db.execute('SELECT id,ticker FROM companies').fetchone()
    calls=[]
    class Provider:
        source, feed = 'massive','sip'
        def bars(self,symbol,start,end):
            calls.append((start,end))
            if len(calls)==1:
                return [bar('2026-10-21T13:30:00Z'),bar('2026-10-21T14:00:00Z')]
            return [bar('2026-10-21T13:45:00Z')]
    try:
        result=collect_company(db, Provider(), row, requested_period(NOW,1),now=NOW,repair_gaps=True)
        assert result.saved == 3 and result.received == 3 and result.gaps_unresolved == 0
        assert len(calls) == 2
        assert calls[1] == (datetime(2026,10,21,13,45,tzinfo=UTC),
                            datetime(2026,10,21,14,0,tzinfo=UTC))
    finally:
        db.close()


def test_gap_repair_never_fabricates_candles_and_can_report_unresolved(tmp_path):
    db=make_db(tmp_path)
    row=db.execute('SELECT id,ticker FROM companies').fetchone()
    class Provider:
        source,feed='massive','sip'
        def bars(self,*_):
            return [bar('2026-10-21T13:30:00Z'),bar('2026-10-21T14:00:00Z')]
    try:
        result=collect_company(db,Provider(),row,requested_period(NOW,1),now=NOW,repair_gaps=True)
        assert result.saved == 2 and result.gaps_unresolved == 1
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0] == 2
    finally:
        db.close()


def test_four_worker_cli_and_sqlite_single_writer(tmp_path, monkeypatch):
    db=make_db(tmp_path,('AAPL','MSFT','NVDA','META'))
    db.close()
    barrier=Barrier(4, timeout=10)
    worker_names=set()
    class FakeMassive:
        source, feed='massive','sip'
        def __init__(self, key): assert key=='TEST_TOKEN'
        def bars(self, symbol, start, end):
            worker_names.add(current_thread().name)
            barrier.wait()
            return [bar('2026-10-21T13:30:00Z')]
    monkeypatch.setenv('MASSIVE_API_KEY','TEST_TOKEN')
    assert run(['--db',str(tmp_path/'merge.sqlite3'),'--source','massive','--last','1',
                '-thread','4','--no-gap-repair'],now=NOW,massive_factory=FakeMassive) == 0
    assert len(worker_names) == 4
    with connect(tmp_path/'merge.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0] == 4


def test_shared_limiter_spreads_requests_across_threads():
    limiter=SharedRateLimiter(.012)
    def started(_):
        limiter.wait()
        return time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        dates = sorted(pool.map(started, range(4)))
    assert all(b-a >= .008 for a,b in zip(dates,dates[1:]))


def test_cron_registration_has_six_jobs_and_massive_script():
    root=Path(__file__).resolve().parents[1]
    assert FIELDS[-1]=='ATLAS_CRON_MASSIVE_PRICES_HOURS'
    assert len(FIELDS)==6
    cron=(root/'cron_job.sh').read_text()
    assert 'extract_massive_prices|' in cron
    assert 'flock -n 9' in cron
    installer=(root.parent/'backend.sh').read_text()
    assert 'run_ai_news_scoring extract_massive_prices' in installer


def test_backend_crontab_install_remove_preserves_unrelated(tmp_path):
    """Run the real installer with isolated fake crontab, never editing host cron."""
    import os
    import shutil
    import subprocess
    import sys
    source=Path(__file__).resolve().parents[1]
    project=tmp_path/'project'
    backend=project/'main_back_end'
    shutil.copytree(source, backend, ignore=shutil.ignore_patterns('__pycache__','*.pyc','tests', 'assets', 'examples'))
    project.mkdir(exist_ok=True)
    shutil.copy2(source.parent/'backend.sh',project/'backend.sh')
    java=project/'android_back_and_java'
    java.mkdir()
    (java/'pom.xml').write_text('<!-- cron-install only needs the file present -->')
    build=project/'runtime/build/backend'
    packages=build/'python-packages'
    packages.mkdir(parents=True)
    (backend/'.env').write_text(f'ATLAS_BACKEND_BUILD_ROOT={build}\n'
                                f'ATLAS_BACKEND_PYTHON_PACKAGES={packages}\n'
                                f'ATLAS_BACKEND_PYTHON=/usr/bin/python3\n'
                                'ATLAS_CRON_MASSIVE_PRICES_HOURS=12\n')
    bin_dir=tmp_path/'bin'
    bin_dir.mkdir()
    cron_file=tmp_path/'fake_crontab'
    initial='15 7 * * * /usr/bin/true # unrelated-job\n'
    cron_file.write_text(initial)
    stub=bin_dir/'crontab'
    stub.write_text('''#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == '-l' ]]; then cat "$ATLAS_TEST_CRON_FILE"
elif [[ "$1" == '-r' ]]; then : > "$ATLAS_TEST_CRON_FILE"
else cp -- "$1" "$ATLAS_TEST_CRON_FILE"
fi
''')
    stub.chmod(0o755)
    env=dict(os.environ,PATH=str(bin_dir)+':'+os.environ['PATH'],
             ATLAS_TEST_CRON_FILE=str(cron_file))
    for action in ('cron-install','cron-install'):
        res=subprocess.run(['bash',str(project/'backend.sh'),action],env=env,
                           capture_output=True,text=True)
        assert res.returncode == 0, res.stderr
        lines=cron_file.read_text().splitlines()
        assert len(lines)==7
        assert lines[0]==initial.strip()
        assert len([line for line in lines if 'ATLAS_MANAGED_CRON:' in line])==6
        assert any('extract_massive_prices 12' in line for line in lines)
    res=subprocess.run(['bash',str(project/'backend.sh'),'cron-remove'],env=env,
                       capture_output=True,text=True)
    assert res.returncode==0, res.stderr
    assert cron_file.read_text()==initial


def test_missing_gap_strict_mode_reports_failure_without_repair(tmp_path, monkeypatch):
    db=make_db(tmp_path)
    db.close()
    class Partial:
        source, feed='massive','sip'
        def __init__(self, key): assert key=='TEST_TOKEN'
        def bars(self, *_):
            return [bar('2026-10-21T13:30:00Z'),bar('2026-10-21T14:00:00Z')]
    monkeypatch.setenv('MASSIVE_API_KEY','TEST_TOKEN')
    assert run(['--db',str(tmp_path/'merge.sqlite3'),'--source','massive',
                '--last','1','--strict-gaps','--no-gap-repair'],
               now=NOW,massive_factory=Partial) == 1


def test_merge_triggers_and_atomic_transaction(tmp_path):
    db=make_db(tmp_path)
    try:
        with db:
            db.execute('CREATE TABLE price_audit (company_id INTEGER, bar_time TEXT)')
            db.execute('''CREATE TRIGGER audit_price AFTER INSERT ON price_bars BEGIN
              INSERT INTO price_audit VALUES (NEW.company_id,NEW.bar_time_utc);
            END''')
        a='2026-10-21T13:30:00Z'
        start=datetime(2026,10,21,tzinfo=UTC)
        assert ingest_bars(db,1,[bar(a),bar(a)],start,NOW,'sip',now=NOW,source='massive') == 1
        assert db.execute('SELECT COUNT(*) FROM price_audit').fetchone()[0] == 1
        assert ingest_bars(db,1,[bar(a)],start,NOW,'sip',now=NOW,source='massive') == 0
        assert db.execute('SELECT COUNT(*) FROM price_audit').fetchone()[0] == 1
    finally:
        db.close()
