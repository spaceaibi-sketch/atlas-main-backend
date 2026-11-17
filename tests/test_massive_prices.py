"""Massive price ingestion with mocked HTTP; never contacts real markets."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from requests import RequestException

from price_pipeline.massive import MassiveClient, MassiveError
from price_pipeline.cli import run
from price_pipeline.collector import collect_company, requested_period
from sec_edgar.database import connect
from news_pipeline.store import ensure_schema
from dashboard.mobile_summary import SCHEMA
from scripts.migrate_mobile_impact_summary import migrate

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
T2 = datetime(2026, 10, 22, 13, 45, tzinfo=UTC)


def sample(t, close=101, *, volume=100):
    return {'t': int(t.timestamp() * 1000), 'o': 100, 'h': 103,
            'l': 99, 'c': close, 'v': volume, 'n': 5, 'vw': 101.25}


class Response:
    def __init__(self, data, code=200, headers=None):
        self.data = data
        self.status_code = code
        self.headers = headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, *responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(session, **kwargs):
    return MassiveClient('PRIVATE_TEST_KEY', session=session, min_interval=0,
                         sleep=lambda _: None, clock=lambda: 100, **kwargs)


def test_massive_requests_raw_15m_bars_and_exact_utc_limits():
    session = Session(Response({'status': 'OK', 'queryCount': 2,
        'results': [sample(T1), sample(T2), sample(NOW)]}))
    out = client(session).bars('aapl', NOW - timedelta(days=2), NOW)
    assert len(out) == 2
    assert out[0] == {'t': '2026-10-22T13:30:00Z', 'o': 100, 'h': 103,
                      'l': 99, 'c': 101, 'v': 100, 'n': 5, 'vw': 101.25}
    url, params, timeout = session.calls[0]
    assert '/ticker/AAPL/range/15/minute/' in url
    assert str(int((NOW-timedelta(days=2)).timestamp()*1000)) in url
    assert str(int(NOW.timestamp()*1000)) in url
    assert params == {'adjusted': 'false', 'sort': 'asc', 'limit': 50000}
    assert session.headers['Authorization'] == 'Bearer PRIVATE_TEST_KEY'
    assert 'PRIVATE_TEST_KEY' not in str(session.calls)
    assert timeout[0] > 0 and timeout[1] > 0



def test_massive_aligns_rolling_request_anchor_without_changing_requested_window():
    # The caller's rolling lookback need not start at a quarter-hour. The
    # vendor must be requested from a canonical anchor, but bars outside the
    # exact caller window must still be excluded from the returned data.
    start = datetime(2026, 10, 22, 13, 22, 43, tzinfo=UTC)
    end = datetime(2026, 10, 22, 14, 10, 12, tzinfo=UTC)
    start_before = datetime(2026, 10, 22, 13, 15, tzinfo=UTC)
    b1 = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
    b2 = datetime(2026, 10, 22, 13, 45, tzinfo=UTC)
    b3 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    after_end = datetime(2026, 10, 22, 14, 15, tzinfo=UTC)
    session = Session(Response({'status': 'OK', 'results': [
        sample(t) for t in (start_before, b1, b2, b3, after_end)
    ]}))
    out = client(session).bars('AAPL', start, end)
    path = session.calls[0][0]
    assert path.endswith(
        f'/range/15/minute/{int(start_before.timestamp() * 1000)}/{int(end.timestamp() * 1000)}'
    )
    assert [bar['t'] for bar in out] == [
        '2026-10-22T13:30:00Z', '2026-10-22T13:45:00Z', '2026-10-22T14:00:00Z'
    ]


def test_multiple_pages_follow_safe_cursor_and_keep_all():
    next_url = (f'https://api.massive.com/v2/aggs/ticker/AAPL/range/15/minute/'
                f'{int((NOW-timedelta(days=3)).timestamp()*1000)}/{int(NOW.timestamp()*1000)}?cursor=abc')
    session = Session(Response({'status': 'OK', 'results': [sample(T1)], 'next_url': next_url}),
                      Response({'status': 'OK', 'results': [sample(T2)], 'resultsCount': 1}))
    out = client(session).bars('AAPL', NOW-timedelta(days=3), NOW)
    assert [r['t'] for r in out] == ['2026-10-22T13:30:00Z', '2026-10-22T13:45:00Z']
    assert session.calls[1][0] == next_url and session.calls[1][1] is None


@pytest.mark.parametrize('next_url', [
    'https://evil.example/v2/aggs/ticker/AAPL/range/15/minute/foo',
    'http://api.massive.com/v2/aggs/ticker/AAPL/range/15/minute/foo',
    'https://api.massive.com:invalid/v2/aggs/ticker/AAPL/range/15/minute/foo',
    'https://api.massive.com.evil.example/v2/aggs/ticker/AAPL/range/15/minute/foo',
])
def test_rejects_untrusted_pagination_urls(next_url):
    session = Session(Response({'status': 'OK', 'results': [], 'next_url': next_url}))
    with pytest.raises(MassiveError, match='unsafe pagination'):
        client(session).bars('AAPL', NOW-timedelta(days=1), NOW)
    assert len(session.calls) == 1


def test_empty_valid_response_and_bad_status_not_confused():
    assert client(Session(Response({'status': 'OK', 'resultsCount': 0}))).bars(
        'ZZZZ', NOW-timedelta(days=1), NOW) == []
    with pytest.raises(MassiveError, match='invalid/failed status'):
        client(Session(Response({'status': 'NOT_AUTHORIZED', 'message': 'bad key'}))).bars(
            'AAPL', NOW-timedelta(days=1), NOW)
    with pytest.raises(MassiveError, match='missing results'):
        client(Session(Response({'status': 'OK'}))).bars('AAPL', NOW-timedelta(days=1), NOW)


def test_credentials_and_transient_failure_are_sanitized():
    with pytest.raises(MassiveError, match='credentials/plan'):
        client(Session(Response({'message': 'private-key-here'}, code=403))).bars(
            'AAPL', NOW-timedelta(days=1), NOW)
    s = Session(Response({'status': 'ERROR'}, code=429, headers={'Retry-After': '1'}),
                Response({'status': 'OK', 'results': [sample(T1)]}))
    assert len(client(s).bars('AAPL', NOW-timedelta(days=1), NOW)) == 1
    assert len(s.calls) == 2
    with pytest.raises(MassiveError, match='network error') as exc:
        client(Session(*[RequestException('token=private-key-here')] * 3)).bars(
            'AAPL', NOW-timedelta(days=1), NOW)
    assert 'private-key-here' not in str(exc.value)


def test_incomplete_50000_base_bars_without_cursor_fails_closed():
    s = Session(Response({'status': 'OK', 'results': [sample(T1)], 'queryCount': 50000}))
    with pytest.raises(MassiveError, match='reduce --last'):
        client(s).bars('AAPL', NOW-timedelta(days=1), NOW)


def test_collector_saves_massive_without_feed_migration_and_mobile_triggers(tmp_path):
    db = connect(tmp_path / 'prices.sqlite3')
    ensure_schema(db)
    with db:
        db.execute("INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES('1','Apple','AAPL',?,?)",
                   ('2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'))
    db.executescript(SCHEMA)
    with db:
        migrate(db, True)
    first = client(Session(Response({'status': 'OK', 'results': [sample(T1), sample(T2)]})))
    row = db.execute("SELECT id,ticker FROM companies WHERE ticker='AAPL'").fetchone()
    result = collect_company(db, first, row, requested_period(NOW, 2), now=NOW)
    assert result.saved == 2 and result.received == 2
    assert [tuple(r) for r in db.execute('SELECT source,feed FROM price_bars')] == [
        ('massive', 'sip'), ('massive', 'sip')]
    assert db.execute('SELECT last_price FROM summary WHERE company_id=?',
                      (row['id'],)).fetchone()[0] == 101
    assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                      "AND name LIKE 'atlas_mob_price_%'").fetchone()[0] == 3
    second = client(Session(Response({'status':'OK', 'results':[sample(T1, close=102)]})))
    assert collect_company(db, second, row, requested_period(NOW, 2), now=NOW).saved == 0
    assert db.execute('SELECT close FROM price_bars WHERE bar_time_utc=?',
                      ('2026-10-22T13:30:00Z',)).fetchone()[0] == 101
    db.close()


def test_massive_cli_dry_run_and_provider_selection(tmp_path, capsys, monkeypatch):
    db = connect(tmp_path / 'cli.sqlite3')
    with db:
        db.execute("INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES('1','Apple','AAPL',?,?)",
                   ('2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'))
    db.close()
    monkeypatch.delenv('MASSIVE_API_KEY', raising=False)
    assert run(['--db', str(tmp_path/'cli.sqlite3'), '--source', 'massive', '--last', '7',
                '--limit', '1', '--dry-run'], now=NOW) == 0
    assert 'source=massive' in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        run(['--source', 'massive', '--last', '2', '--feed', 'sip', '--dry-run'], now=NOW)
    assert error.value.code == 2
    assert run(['--db', str(tmp_path/'cli.sqlite3'), '--source', 'massive',
                '--last', '1', '--limit', '1'], now=NOW) == 2
    assert 'Missing MASSIVE_API_KEY' in capsys.readouterr().err


def test_massive_cli_with_mocked_source(tmp_path, monkeypatch):
    db = connect(tmp_path / 'cli.sqlite3')
    with db:
        db.execute("INSERT INTO companies(cik,name,ticker,created_at,updated_at) VALUES('1','Apple','AAPL',?,?)",
                   ('2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'))
    db.close()
    monkeypatch.setenv('MASSIVE_API_KEY', 'SECRET_TEST')
    class FakeSource:
        source, feed = 'massive', 'sip'
        def __init__(self, key):
            assert key == 'SECRET_TEST'
        def bars(self, symbol, start, end):
            assert symbol == 'AAPL'
            return [{'t': '2026-10-22T13:30:00Z', 'o': 100, 'h': 103, 'l': 99,
                     'c': 101, 'v': 100, 'n': 5, 'vw': 101.25}]
    assert run(['--db', str(tmp_path/'cli.sqlite3'), '--source', 'massive',
                '--last', '2', '--limit', '1'], now=NOW, massive_factory=FakeSource) == 0
    with connect(tmp_path/'cli.sqlite3') as db:
        assert tuple(db.execute('SELECT source,feed FROM price_bars').fetchone()) == ('massive','sip')


def test_massive_client_preserves_fractional_share_volume():
    session = Session(Response({'status': 'OK', 'results': [
        sample(T1, volume=24711.700913), sample(T2, volume=99.5)
    ]}))
    rows = client(session).bars('AAPL', NOW - timedelta(days=1), NOW)
    assert [row['v'] for row in rows] == [24711.700913, 99.5]
    assert all(isinstance(row['v'], float) for row in rows)


def test_massive_ohlc_without_volume_is_preserved_as_price_only():
    raw=sample(T1)
    raw.pop('v')
    raw.pop('n')
    raw.pop('vw')
    result=client(Session(Response({'status':'OK','results':[raw]}))).bars(
        'AAPL', NOW - timedelta(days=1), NOW)
    assert result == [{'t':'2026-10-22T13:30:00Z','o':100,'h':103,
                       'l':99,'c':101,'v':None,'n':None,'vw':None}]
