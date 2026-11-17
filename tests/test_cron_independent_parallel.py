"""Regression tests: hourly pipelines can overlap without duplicate job runs."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(tmp_path):
    root = tmp_path / 'main_back_end'
    root.mkdir()
    shutil.copyfile(ROOT / 'cron_job.sh', root / 'cron_job.sh')
    for name in ('extract_forms_items', 'extract_news',
                 'run_ai_sec_scoring', 'run_ai_sec_analys'):
        (root / f'{name}.sh').write_text(
            '#!/usr/bin/env bash\n'
            'set -euo pipefail\n'
            'touch "$ATLAS_TEST_STATE/${0##*/}.started"\n'
            'while [[ ! -e "$ATLAS_TEST_STATE/release" ]]; do sleep .03; done\n'
            'echo "$0" >> "$ATLAS_TEST_STATE/completed"\n'
        )
    return root


def _command(root, name, state):
    env = dict(os.environ, ATLAS_TEST_STATE=str(state))
    return ['bash', str(root / 'cron_job.sh'), name, '1'], env


def _until(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f'{path.name} was never started'
        time.sleep(.015)


def test_different_scheduled_jobs_run_at_the_same_time(backend, tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    cmds = [_command(backend, name, state) for name in ('extract_forms_items', 'extract_news')]
    processes = []
    try:
        for cmd, env in cmds:
            processes.append(subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for name in ('extract_forms_items', 'extract_news'):
            _until(state / f'{name}.sh.started')
        assert all(proc.poll() is None for proc in processes)
        assert not (state / 'completed').exists()
        (state / 'release').touch()
        for proc in processes:
            out, err = proc.communicate(timeout=5)
            assert proc.returncode == 0, (out, err)
        assert len((state / 'completed').read_text().splitlines()) == 2
        assert (tmp_path / 'data/cron_state/extract_forms_items.last_success').exists()
        assert (tmp_path / 'data/cron_state/extract_news.last_success').exists()
    finally:
        (state / 'release').touch()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


def test_same_scheduled_job_cannot_overlap(backend, tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    cmd, env = _command(backend, 'extract_news', state)
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _until(state / 'extract_news.sh.started')
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0
        assert 'skipped (same job already running)' in result.stdout
        assert not (tmp_path / 'data/cron_state/extract_news.last_success').exists()
        (state / 'release').touch()
        out, err = p.communicate(timeout=5)
        assert p.returncode == 0, (out, err)
        assert len((state / 'completed').read_text().splitlines()) == 1
    finally:
        (state / 'release').touch()
        if p.poll() is None:
            p.kill()
            p.communicate()


def test_legacy_alias_and_canonical_share_lock_and_success_state(backend, tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    old_cmd, env = _command(backend, 'run_ai_sec_analys', state)
    current_cmd, _ = _command(backend, 'run_ai_sec_scoring', state)
    p = subprocess.Popen(old_cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _until(state / 'run_ai_sec_analys.sh.started')
        result = subprocess.run(current_cmd, env=env, capture_output=True, text=True, timeout=5)
        assert 'skipped (same job already running)' in result.stdout
        (state / 'release').touch()
        out, err = p.communicate(timeout=5)
        assert p.returncode == 0, (out, err)
        assert (tmp_path / 'data/cron_state/run_ai_sec_scoring.last_success').exists()
        result = subprocess.run(current_cmd, env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0
        assert len((state / 'completed').read_text().splitlines()) == 1
    finally:
        (state / 'release').touch()
        if p.poll() is None:
            p.kill()
            p.communicate()

@pytest.mark.parametrize('wrapper', (
    'extract_recent_filings.sh', 'extract_forms_items.sh', 'extract_news.sh',
))
def test_temporary_lock_skip_does_not_rebuild_summary(wrapper, tmp_path):
    root = tmp_path / 'main_back_end'
    (root / 'scripts').mkdir(parents=True)
    shutil.copyfile(ROOT / wrapper, root / wrapper)
    (root / 'scripts/run_python.sh').write_text(
        '#!/usr/bin/env bash\n'
        'echo "$*" >> "$ATLAS_TEST_CALLS"\n'
        'exit 75\n'
    )
    calls = tmp_path / 'calls'
    env = dict(os.environ, ATLAS_TEST_CALLS=str(calls))
    result = subprocess.run(['bash', str(root / wrapper)], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 75
    assert len(calls.read_text().splitlines()) == 1
    assert 'dashboard.mobile_summary' not in calls.read_text()


def test_canonical_sqlite_writer_waits_for_other_job(tmp_path, monkeypatch):
    """Independent jobs must use SQLite's writer arbitration, not corrupt rows."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sec_edgar.database import connect

    monkeypatch.setenv('ATLAS_SQLITE_BUSY_TIMEOUT_MS', '5000')
    path = tmp_path / 'writes.sqlite3'
    with connect(path) as seed:
        seed.execute("INSERT INTO companies(cik,name,created_at,updated_at) VALUES('0000000001','A','t','t')")
    started = Event()

    def write_in_second_job():
        with connect(path) as other:
            started.set()
            other.execute('BEGIN IMMEDIATE')
            other.execute("UPDATE companies SET name='B' WHERE cik='0000000001'")

    with connect(path) as first, ThreadPoolExecutor(max_workers=1) as pool:
        first.execute('BEGIN IMMEDIATE')
        first.execute("UPDATE companies SET name='A1' WHERE cik='0000000001'")
        task = pool.submit(write_in_second_job)
        assert started.wait(timeout=3)
        assert not task.done()  # Second job is waiting to write, not failing.
        first.commit()
        task.result(timeout=5)
    with connect(path) as verify:
        assert verify.execute('SELECT name FROM companies').fetchone()[0] == 'B'
