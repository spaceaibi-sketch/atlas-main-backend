"""Offline, exact diagnostic accounting for Massive 15-minute price ingestion."""
from __future__ import annotations

from datetime import datetime, timezone

from price_pipeline.storage import ingest_bars, normalize_bars
from price_pipeline.collector import collect_company, requested_period
from sec_edgar.database import connect

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)
START = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)


def bar(t: str, **overrides):
    return {'t': t, 'o': 100, 'h': 103, 'l': 99, 'c': 101,
            'v': 100, 'n': 5, 'vw': 100.5, **overrides}


def db_with_company(tmp_path):
    conn = connect(tmp_path / 'test_prices.sqlite3')
    with conn:
        conn.execute("""INSERT INTO companies(cik,name,ticker,created_at,updated_at)
            VALUES('1','Apple','AAPL','2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')""")
    return conn


def test_diagnostics_breakdown_exclusive_and_accounting():
    rows = [
        bar('2026-10-22T13:30:00Z'),
        bar('2026-10-22T13:30:00Z', c=102),  # repeated key, first wins
        None,                                  # non-object
        bar('2026-10-22T13:45:00'),            # timezone absent
        bar('nonsense'),                      # invalid timestamp but TZ also absent
        bar('nonsenseZ'),                     # unparseable after timezone present
        bar('2026-10-20T13:45:00Z'),            # outside requested window
        bar('2026-10-22T13:37:00Z'),            # off 15-minute boundary
        bar('2026-10-22T14:00:00Z', h=98),      # OHLC inequality
        bar('2026-10-22T14:15:00Z', v=1.5),     # fractional volume
        bar('2026-10-22T14:30:00Z', n=-1),      # negative count
        bar('2026-10-22T14:45:00Z', vw=-1),     # negative VWAP
    ]
    diag = {'batch': 'initial'}
    frame, rejected = normalize_bars(rows, START, NOW, diagnostics=diag)
    assert frame['t'].tolist() == ['2026-10-22T13:30:00Z']
    assert rejected == 11
    assert diag['rejected_invalid'] == 10
    assert diag['duplicates_in_response'] == 1
    assert diag['valid_unique'] == 1
    assert diag['rejection_reasons'] == {
        'non_object': 1,
        'timestamp_missing_timezone': 2,
        'timestamp_unparseable': 1,
        'timestamp_outside_window': 1,
        'timestamp_not_15m': 1,
        'invalid_ohlc': 1,
        'invalid_volume': 1,
        'invalid_trade_count': 1,
        'invalid_vwap': 1,
    }
    assert sum(diag['rejection_reasons'].values()) == diag['rejected_invalid']
    assert diag['received'] == diag['rejected_invalid'] + diag['duplicates_in_response'] + diag['valid_unique']


def test_existing_keys_are_attributed_by_actual_stored_source_and_valid_new_inserted(tmp_path):
    db = db_with_company(tmp_path)
    a, b, c = '2026-10-22T13:30:00Z', '2026-10-22T13:45:00Z', '2026-10-22T14:00:00Z'
    try:
        # One older row from Alpaca, one from Massive, one genuinely missing.
        assert ingest_bars(db, 1, [bar(a)], START, NOW,
                           'sip', now=NOW, source='alpaca') == 1
        assert ingest_bars(db, 1, [bar(b)], START, NOW,
                           'sip', now=NOW, source='massive') == 1
        diag = {'batch': 'initial'}
        n = ingest_bars(db, 1, [bar(a, c=102), bar(b, c=102), bar(c), bar(c, c=102)],
                        START, NOW, 'sip', now=NOW, source='massive', diagnostics=diag)
        assert n == 1
        assert diag['received'] == 4
        assert diag['valid_unique'] == 3
        assert diag['rejected_invalid'] == 0
        assert diag['duplicates_in_response'] == 1
        assert diag['existing_keys'] == 2
        assert diag['fresh_candidates'] == 1
        assert diag['saved'] == diag['sqlite_changes'] == 1
        assert diag['company_rows_before'] == 2
        assert diag['company_rows_after'] == 3
        assert diag['missing_after_merge'] == 0
        assert diag['existing_by_source_feed'] == {'alpaca/sip': 1, 'massive/sip': 1}
        assert not diag['merge_mismatch']
        assert [tuple(r) for r in db.execute('SELECT source,close FROM price_bars ORDER BY bar_time_utc')] == [
            ('alpaca', 101), ('massive', 101), ('massive', 101)]
        rerun = {}
        assert ingest_bars(db, 1, [bar(a), bar(b), bar(c)], START, NOW,
                           'sip', now=NOW, source='massive', diagnostics=rerun) == 0
        assert rerun['existing_keys'] == 3
        assert rerun['fresh_candidates'] == 0
        assert rerun['company_rows_before'] == rerun['company_rows_after'] == 3
        assert rerun['missing_after_merge'] == 0
        assert not rerun['merge_mismatch']
    finally:
        db.close()


def test_no_valid_bars_appear_as_rejections_not_sqlite_conflicts(tmp_path):
    db = db_with_company(tmp_path)
    try:
        diag = {}
        assert ingest_bars(db, 1, [bar('2026-10-22T14:01:00Z')], START, NOW,
                           'sip', now=NOW, source='massive', diagnostics=diag) == 0
        assert diag['rejected_invalid'] == 1
        assert diag['rejection_reasons']['timestamp_not_15m'] == 1
        assert diag['existing_keys'] == diag['fresh_candidates'] == diag['saved'] == 0
        assert diag['company_rows_before'] == diag['company_rows_after'] == 0
        assert not diag['merge_mismatch']
    finally:
        db.close()


def test_collector_records_initial_and_repair_diagnostics(tmp_path, monkeypatch):
    import price_pipeline.collector as collector
    from datetime import timedelta
    db = db_with_company(tmp_path)
    class Provider:
        source, feed = 'massive', 'sip'
        def __init__(self): self.calls = []
        def bars(self, symbol, start, end):
            self.calls.append((symbol, start, end))
            if len(self.calls) == 1:
                return [bar('2026-10-22T13:30:00Z'), bar('2026-10-22T14:00:00Z')]
            return [bar('2026-10-22T13:45:00Z')]
    try:
        provider = Provider()
        row = db.execute('SELECT id,ticker FROM companies').fetchone()
        result = collect_company(db, provider, row, requested_period(NOW, 1),
                                 now=NOW, repair_gaps=True)
        assert result.saved == 3
        assert [d['batch'] for d in result.diagnostic_batches] == ['initial', 'gap_repair']
        assert [d['saved'] for d in result.diagnostic_batches] == [2, 1]
        assert all(d['fresh_candidates'] == d['saved'] and not d['merge_mismatch']
                   for d in result.diagnostic_batches)
    finally:
        db.close()


def test_empty_and_nonobject_batch_diagnostics(tmp_path):
    db = db_with_company(tmp_path)
    try:
        for bars, expected_invalid in (([], 0), ([None, 17], 2)):
            diag = {}
            assert ingest_bars(db, 1, bars, START, NOW, 'sip',
                               now=NOW, source='massive', diagnostics=diag) == 0
            assert diag['received'] == len(bars)
            assert diag['rejected_invalid'] == expected_invalid
            assert diag['valid_unique'] == diag['fresh_candidates'] == 0
            assert diag['existing_keys'] == diag['saved'] == 0
            assert diag['first_valid_utc'] is None and diag['last_valid_utc'] is None
            assert not diag['merge_mismatch']
    finally:
        db.close()


def test_massive_cli_reports_zero_saved_as_existing_not_unclassified(tmp_path, monkeypatch, capsys):
    from price_pipeline.cli import run
    db = db_with_company(tmp_path)
    db_path = tmp_path / 'test_prices.sqlite3'
    time = '2026-10-22T13:30:00Z'
    try:
        assert ingest_bars(db, 1, [bar(time)], START, NOW,
                           'sip', now=NOW, source='alpaca') == 1
    finally:
        db.close()

    class FakeMassive:
        source, feed = 'massive', 'sip'
        def __init__(self, _key): pass
        def bars(self, *_args):
            return [bar(time), bar('2026-10-22T13:37:00Z')]

    monkeypatch.setenv('MASSIVE_API_KEY', 'FAKE_TEST_KEY')
    assert run(['--db', str(db_path), '--source', 'massive', '--last', '1',
                '--symbol', 'AAPL', '--no-gap-repair', '--threads', '1'],
               now=NOW, massive_factory=FakeMassive) == 0
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith('[PRICE DIAG] AAPL'))
    for expected in ('received=2', 'valid_unique=1', 'invalid=1',
                     'existing_keys=1', 'fresh_candidates=0', 'saved=0',
                     'existing_by_source=alpaca/sip:1',
                     'rejected_by_reason=timestamp_not_15m:1'):
        assert expected in line, (expected, line)
    assert '[PRICE DIAG] TOTAL received=2 valid_unique=1 rejected_invalid=1' in out
    with connect(db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0] == 1


def test_massive_price_only_ignores_invalid_market_metadata_not_prices(tmp_path):
    """Massive OHLC bars must survive invalid/missing volume, trades and VWAP."""
    db = db_with_company(tmp_path)
    times = [f'2026-10-22T{h:02d}:{m:02d}:00Z' for h,m in
             [(13,30),(13,45),(14,0),(14,15),(14,30),(14,45),(15,0),(15,15)]]
    no_volume = bar(times[7])
    no_volume.pop('v')
    no_volume.pop('n')
    no_volume.pop('vw')
    rows = [
        bar(times[0], v=24711.700913),
        bar(times[1], v=99),
        bar(times[2], v=-0.25),
        bar(times[3], v=float('inf')),
        bar(times[4], v=None),
        bar(times[5], v=0.5, n=1.25),
        bar(times[6], v='broken', n='broken', vw=-1),
        no_volume,
        bar('2026-10-22T15:30:00Z', o=float('nan')),
        bar('2026-10-22T15:37:00Z'),
    ]
    try:
        diag = {}
        assert ingest_bars(db, 1, rows, START, NOW, 'sip', now=NOW,
                           source='massive', diagnostics=diag) == 8
        assert diag['valid_unique'] == 8
        assert diag['rejected_invalid'] == 2
        assert diag['rejection_reasons']['invalid_ohlc'] == 1
        assert diag['rejection_reasons']['timestamp_not_15m'] == 1
        assert diag['rejection_reasons']['invalid_volume'] == 0
        assert diag['rejection_reasons']['invalid_trade_count'] == 0
        assert diag['rejection_reasons']['invalid_vwap'] == 0
        assert diag['volume_defaulted'] == 5
        assert diag['trade_count_ignored'] == 2
        assert diag['vwap_ignored'] == 1
        assert diag['saved'] == diag['sqlite_changes'] == diag['fresh_candidates'] == 8
        assert not diag['merge_mismatch']
        stored = list(db.execute("""SELECT bar_time_utc,close,volume,
                                      typeof(volume),trade_count,vwap
                                    FROM price_bars ORDER BY bar_time_utc"""))
        assert [r['bar_time_utc'] for r in stored] == times
        assert [r['close'] for r in stored] == [101] * 8
        assert [r['volume'] for r in stored] == [24711.700913,99,0,0,0,0.5,0,0]
        assert [r['trade_count'] for r in stored][5:] == [None,None,None]
        assert stored[6]['vwap'] is None and stored[7]['vwap'] is None
        assert stored[0]['vwap'] == 100.5
        assert stored[0]['typeof(volume)'] == 'real'
        # Idempotence: existing OHLC values cannot be overwritten by a rerun.
        changed = [bar(times[0], c=102, v=None)]
        assert ingest_bars(db, 1, changed, START, NOW, 'sip', now=NOW,
                           source='massive') == 0
        assert db.execute('SELECT close FROM price_bars WHERE bar_time_utc=?',
                          (times[0],)).fetchone()[0] == 101
    finally:
        db.close()


def test_other_sources_remain_strict_about_fractional_volume(tmp_path):
    db = db_with_company(tmp_path)
    try:
        diag = {}
        assert ingest_bars(db, 1, [bar('2026-10-22T13:30:00Z', v=2.5)],
                           START, NOW, 'sip', now=NOW, source='alpaca',
                           diagnostics=diag) == 0
        assert diag['rejection_reasons']['invalid_volume'] == 1
        assert diag['saved'] == 0
    finally:
        db.close()
