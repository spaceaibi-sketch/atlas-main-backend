"""Regression tests for the dedicated Massive all-companies launcher (offline)."""
from __future__ import annotations

import pytest

from sec_edgar.database import connect
from price_pipeline.massive_cli import main, massive_arguments


def test_dedicated_launcher_forces_massive_and_defaults_to_all_seven_days():
    assert massive_arguments([]) == ['--source', 'massive', '--last', '7']
    assert massive_arguments(['--dry-run']) == ['--source', 'massive', '--last', '7', '--dry-run']
    assert massive_arguments(['--last', '30']) == ['--source', 'massive', '--last', '30']
    assert massive_arguments(['--last=14']) == ['--source', 'massive', '--last=14']
    with pytest.raises(ValueError, match='Massive-only'):
        massive_arguments(['--source', 'alpaca'])
    with pytest.raises(ValueError, match='Massive-only'):
        massive_arguments(['--source=yfinance'])


def test_all_symbols_dry_run_reads_all_companies_by_default(tmp_path, capsys, monkeypatch):
    dbpath = tmp_path / 'companies.db'
    with connect(dbpath) as db:
        with db:
            for i, ticker in enumerate(('AAPL', 'MSFT', 'NVDA', None, ''), 1):
                db.execute('''INSERT INTO companies(cik,name,ticker,created_at,updated_at)
                    VALUES(?,?,?,?,?)''', (str(i), f'Company {i}', ticker,
                                      '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z'))
    # Configured PRICE_SOURCE must not override this dedicated launcher.
    monkeypatch.setenv('PRICE_SOURCE', 'alpaca')
    monkeypatch.delenv('MASSIVE_API_KEY', raising=False)
    assert main(['--db', str(dbpath), '--dry-run']) == 0
    output = capsys.readouterr().out
    assert 'source=massive' in output
    assert 'last=7d' in output
    assert 'Selected 3 companies' in output
    for ticker in ('AAPL', 'MSFT', 'NVDA'):
        assert f'PLAN {ticker} ' in output
    assert 'Completed companies=3' in output


def test_override_days_and_optional_debug_limit(tmp_path, capsys):
    dbpath = tmp_path / 'companies.db'
    with connect(dbpath) as db:
        with db:
            for i, ticker in enumerate(('AAPL', 'MSFT'), 1):
                db.execute('''INSERT INTO companies(cik,name,ticker,created_at,updated_at)
                    VALUES(?,?,?,?,?)''', (str(i), f'Company {i}', ticker,
                                      '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z'))
    assert main(['--db', str(dbpath), '--last', '12', '--limit', '1', '--dry-run']) == 0
    output = capsys.readouterr().out
    assert 'last=12d' in output
    assert 'Selected 1 companies' in output


def test_dedicated_launcher_refuses_provider_switch(capsys):
    assert main(['--source', 'alpaca']) == 2
    assert 'Massive-only launcher' in capsys.readouterr().err


def test_all_company_collection_writes_each_company_without_per_company_flags(
        tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    from price_pipeline import massive_cli
    from price_pipeline.cli import run as shared_run

    dbpath = tmp_path / 'companies.db'
    with connect(dbpath) as db:
        with db:
            for i, ticker in enumerate(('AAPL', 'MSFT', 'NVDA'), 1):
                db.execute('''INSERT INTO companies(cik,name,ticker,created_at,updated_at)
                    VALUES(?,?,?,?,?)''', (str(i), f'Company {i}', ticker,
                                      '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z'))
    now = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)
    captured = []

    class FakeMassive:
        source, feed = 'massive', 'sip'

        def __init__(self, key):
            assert key == 'TEST_KEY'

        def bars(self, symbol, start, end):
            captured.append(symbol)
            assert start == now - timedelta(days=7)
            assert end == now
            return [{'t': '2026-10-22T15:45:00Z',
                     'o': 100, 'h': 102, 'l': 99, 'c': 101,
                     'v': 500, 'vw': 101, 'n': 2}]

    monkeypatch.setenv('MASSIVE_API_KEY', 'TEST_KEY')
    monkeypatch.setattr(massive_cli, 'run', lambda args: shared_run(
        args, now=now, massive_factory=FakeMassive))
    assert massive_cli.main(['--db', str(dbpath)]) == 0
    assert set(captured) == {'AAPL', 'MSFT', 'NVDA'}
    assert len(captured) == 3
    with connect(dbpath) as db:
        rows = list(db.execute('''SELECT c.ticker,p.source,p.feed
            FROM price_bars p JOIN companies c ON c.id=p.company_id
            ORDER BY c.ticker'''))
        assert [tuple(row) for row in rows] == [
            ('AAPL', 'massive', 'sip'), ('MSFT', 'massive', 'sip'),
            ('NVDA', 'massive', 'sip')]
    assert 'Completed companies=3' in capsys.readouterr().out
