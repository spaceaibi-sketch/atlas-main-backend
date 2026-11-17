"""Offline service tests; no API key, WebSocket, or production DB required."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from price_pipeline.massive_ws_store import (
    PERIOD_MS, BarSpool, load_company_map, parse_minute, write_pending,
)
from price_pipeline.massive_ws_service import scan_status, authenticate_and_subscribe, RemoteStatusError

# Deliberately aligned UTC quarter-hour slots.
T0 = 1_783_440_000_000  # 2026-07-07, exact time not relevant
T0 = T0 // PERIOD_MS * PERIOD_MS


def minute(n, sym='AAPL', **kw):
    fields = {'ev':'AM','sym':sym,'s':T0+n*60_000,
              'o':100.+n,'h':103.+n,'l':99.+n,'c':102.+n,'v':10+n,'vw':101.+n}
    fields.update(kw)
    return parse_minute(fields)


class FakeWS:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
    def send(self, frame):
        import json
        self.sent.append(json.loads(frame))
    def recv(self):
        return next(self.frames)


class MassiveWsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.target = self.dir / 'live.sqlite3'
        self.spool_path = self.dir / 'spool.sqlite3'
        with sqlite3.connect(self.target) as db:
            db.executescript('''
                CREATE TABLE companies(id INTEGER PRIMARY KEY, ticker TEXT NOT NULL);
                INSERT INTO companies(id,ticker) VALUES (1,'AAPL'), (2,'MSFT');
                CREATE TABLE price_bars (
                    company_id INTEGER NOT NULL, bar_time_utc TEXT NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                    close REAL NOT NULL, volume INTEGER NOT NULL,
                    trade_count INTEGER, vwap REAL, source TEXT NOT NULL,
                    feed TEXT NOT NULL, ingested_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,bar_time_utc));
                CREATE TABLE observe_triggers(n INTEGER NOT NULL);
                INSERT INTO observe_triggers VALUES(0);
                CREATE TRIGGER price_written AFTER INSERT ON price_bars BEGIN
                  UPDATE observe_triggers SET n=n+1;
                END;
            ''')
        self.store = BarSpool(self.spool_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_parse_valid_invalid(self):
        self.assertEqual(minute(0)[1], T0)
        self.assertIsNone(minute(0, v=-1))
        self.assertIsNone(minute(0, o=0))
        self.assertIsNone(minute(0, h=50))
        self.assertIsNone(minute(0, s=T0+1))
        self.assertIsNone(parse_minute({'ev':'T', 'sym':'AAPL'}))

    def test_aggregation_sparse_minutes_and_duplicate_update(self):
        self.store.stage([minute(0), minute(2), minute(14), minute(0, sym='MSFT')])
        # Updated provider minute replaces prior provisional aggregate.
        self.store.stage([minute(2, c=105.0, h=106.0)])
        self.assertEqual(self.store.close_completed(T0 + 14*60_000), 0)
        self.assertEqual(self.store.close_completed(T0 + PERIOD_MS), 2)
        items = self.store.pending()
        apple = next(r for r in items if r[0] == 'AAPL')
        self.assertEqual((apple[2], apple[3], apple[4], apple[5], apple[6]),
                         (100.0, 117.0, 99.0, 116.0, 46))
        expected_vwap = (101*10 + 103*12 + 115*24)/46
        self.assertAlmostEqual(apple[7], expected_vwap)

    def test_write_preserves_rows_and_runs_mobile_trigger(self):
        self.store.stage([minute(0), minute(14)])
        self.store.close_completed(T0+PERIOD_MS)
        with sqlite3.connect(self.target) as db:
            mapping=load_company_map(db)
        self.assertEqual(write_pending(self.target,self.store,mapping), (1,1,0))
        self.assertEqual(write_pending(self.target,self.store,mapping), (0,0,0))
        # Even a retried/replayed earlier slot never modifies the first price.
        self.store.close()
        self.store = BarSpool(self.spool_path)
        self.assertEqual(self.store.pending_count(), 0)
        self.store.conn.execute('''INSERT INTO pending_bars VALUES
            ('AAPL',?,999,1000,998,999,500,NULL)''', (T0,))
        self.store.conn.commit()
        self.assertEqual(write_pending(self.target,self.store,mapping), (1,0,0))
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute('SELECT open FROM price_bars').fetchone()[0], 100.0)
            self.assertEqual(db.execute('SELECT n FROM observe_triggers').fetchone()[0],1)

    def test_busy_database_preserves_spool(self):
        self.store.stage([minute(0)])
        self.store.close_completed(T0+PERIOD_MS)
        with sqlite3.connect(self.target) as db:
            mapping=load_company_map(db)
        with sqlite3.connect(self.target, isolation_level=None) as locked:
            locked.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(sqlite3.OperationalError):
                write_pending(self.target,self.store,mapping)
            locked.rollback()
        self.assertEqual(self.store.pending_count(),1)
        self.assertEqual(write_pending(self.target,self.store,mapping),(1,1,0))

    def test_invalidated_on_reconnect_and_delayed_events(self):
        self.store.stage([minute(0),minute(1)])
        self.store.invalidate(T0,T0)
        self.store.stage([minute(2)])
        self.store.close_completed(T0+PERIOD_MS)
        self.assertEqual(self.store.pending_count(),0)
        self.store.stage([minute(0)])
        self.assertEqual(self.store.close_completed(T0+2*PERIOD_MS),0)

    def test_spool_survives_restart(self):
        self.store.stage([minute(0)])
        self.store.close_completed(T0+PERIOD_MS)
        self.store.close()
        self.store=BarSpool(self.spool_path)
        self.assertEqual(self.store.pending_count(),1)

    def test_two_separate_intervals_and_out_of_order_minutes(self):
        # Arrival order must not change the chronological open and close.
        self.store.stage([minute(14), minute(0), minute(15), minute(17)])
        self.assertEqual(self.store.close_completed(T0+PERIOD_MS), 1)
        first=self.store.pending()[0]
        self.assertEqual(first[2],100.0)
        self.assertEqual(first[5],116.0)
        self.assertEqual(self.store.close_completed(T0+2*PERIOD_MS), 1)
        self.assertEqual(len(self.store.pending()),2)

    def test_unknown_symbol_never_creates_company(self):
        self.store.stage([minute(0,sym='UNKNOWN')])
        self.store.close_completed(T0+PERIOD_MS)
        with sqlite3.connect(self.target) as db:
            mapping=load_company_map(db)
        self.assertEqual(write_pending(self.target,self.store,mapping),(1,0,1))
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM companies').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM price_bars').fetchone()[0],0)

    def test_handshake_waits_for_auth_before_subscribe(self):
        import os
        ws=FakeWS(['[{"ev":"status","status":"connected"}]',
                   '[{"ev":"status","status":"auth_success"}]',
                   '[{"ev":"status","status":"success","message":"subscribed to: AM.*"}]'])
        old=os.environ.get('MASSIVE_API_KEY')
        try:
            os.environ['MASSIVE_API_KEY']='test-secret-not-real'
            authenticate_and_subscribe(ws)
        finally:
            if old is None:
                del os.environ['MASSIVE_API_KEY']
            else:
                os.environ['MASSIVE_API_KEY']=old
        self.assertEqual([r['action'] for r in ws.sent],['auth','subscribe'])
        self.assertEqual(ws.sent[1]['params'],'AM.*')
        with self.assertRaises(RemoteStatusError):
            scan_status('[{"ev":"status","status":"auth_failed"}]')


if __name__=='__main__':
    unittest.main()
