"""Durable, insert-only 15-minute rollups of Massive minute WebSocket aggregates.

Only the private spool schema is created. The live SEC schema, journal mode,
REST API, and all existing producer scripts remain unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import math
import sqlite3
import time

PERIOD_MS = 15 * 60 * 1000
MINUTE_MS = 60 * 1000


def slot_start(ms: int) -> int:
    return ms // PERIOD_MS * PERIOD_MS


def parse_minute(event: dict) -> tuple | None:
    """Accept one *complete* Massive AM minute update, not a raw trade.

    Massive can publish multiple updates for one minute; the latest accepted
    update replaces that minute's spool value until the 15-minute rollup closes.
    """
    if not isinstance(event, dict) or event.get('ev') != 'AM':
        return None
    sym = event.get('sym')
    ms = event.get('s')
    if not isinstance(sym, str) or not sym or len(sym) > 48 or not all(
            ch.isalnum() or ch in '.-/' for ch in sym):
        return None
    if type(ms) is not int or ms < 1_500_000_000_000 or ms % MINUTE_MS:
        return None
    try:
        values = [float(event[k]) for k in ('o', 'h', 'l', 'c')]
        if not all(math.isfinite(x) and x > 0 for x in values):
            return None
        o, h, l, c = values
        if l > min(o, c) or h < max(o, c):
            return None
        vol = event['v']
        if type(vol) is not int or not 0 <= vol < 2**63:
            return None
        vw = event.get('vw')
        if vw is not None:
            vw = float(vw)
            if not math.isfinite(vw) or vw <= 0:
                vw = None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return (sym.upper(), ms, slot_start(ms), o, h, l, c, vol, vw)


class BarSpool:
    """Two-phase spool: minutes -> closed pending bars -> existing SEC DB.

    Live DB writes may fail with SQLITE_BUSY. Pending rows are retained and
    replayed after restart; the target primary key makes repeat writes safe.
    """

    def __init__(self, path: str | Path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p, timeout=10)
        self.conn.execute('PRAGMA journal_mode=WAL')  # private spool, never SEC DB
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('PRAGMA busy_timeout=10000')
        with self.conn:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS minute_events (
                    symbol TEXT NOT NULL,
                    minute_ms INTEGER NOT NULL,
                    slot_ms INTEGER NOT NULL,
                    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
                    volume INTEGER NOT NULL, vw REAL,
                    PRIMARY KEY(symbol, minute_ms)
                );
                CREATE INDEX IF NOT EXISTS idx_massive_ws_minute_slot
                    ON minute_events(slot_ms);
                CREATE TABLE IF NOT EXISTS invalid_windows (
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                    PRIMARY KEY(start_ms,end_ms)
                );
                CREATE TABLE IF NOT EXISTS pending_bars (
                    symbol TEXT NOT NULL, slot_ms INTEGER NOT NULL,
                    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
                    volume INTEGER NOT NULL, vw REAL,
                    PRIMARY KEY(symbol,slot_ms)
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
            ''')

    def close(self):
        self.conn.close()

    def metadata(self, key: str, default: int = 0) -> int:
        row = self.conn.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return int(row[0]) if row else default

    def record_clock(self, wall_ms: int):
        with self.conn:
            self.conn.execute('''INSERT INTO metadata(key,value) VALUES('last_connected_wall_ms',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (wall_ms,))

    def invalidate(self, start_ms: int, end_ms: int):
        """Exclude buckets touched by downtime or initial partial subscription."""
        a, b = slot_start(start_ms), slot_start(end_ms)
        if b < a:
            a, b = b, a
        with self.conn:
            self.conn.execute('INSERT OR IGNORE INTO invalid_windows VALUES (?,?)', (a, b))
            self.conn.execute('DELETE FROM minute_events WHERE slot_ms BETWEEN ? AND ?', (a, b))
            # Already finalized pending bars remain immutable and safe; a
            # failed/incomplete in-progress interval must never be finalized.

    def stage(self, events: list[tuple]) -> int:
        if not events:
            return 0
        closed_before = self.metadata('finalized_before_ms', 0)
        # One event per vendor AM record; SQLite's UPSERT handles revisions.
        # SQL filters prevent back-dated messages from reviving closed or bad bars.
        with self.conn:
            self.conn.executemany('''INSERT INTO minute_events
                (symbol,minute_ms,slot_ms,o,h,l,c,volume,vw)
                SELECT ?,?,?,?,?,?,?,?,?
                WHERE ? >= ? AND NOT EXISTS (
                   SELECT 1 FROM invalid_windows i WHERE ? BETWEEN i.start_ms AND i.end_ms)
                ON CONFLICT(symbol,minute_ms) DO UPDATE SET
                o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,
                volume=excluded.volume,vw=excluded.vw''',
                (r + (r[2], closed_before, r[2]) for r in events))
        return len(events)

    def close_completed(self, cutoff_wall_ms: int) -> int:
        """Finalize exactly closed intervals; never insert partial/current bars.

        cutoff_wall_ms is already adjusted for vendor feed delay + grace.
        Minutes can be sparse (a no-trade minute correctly produces no AM).
        Aggregation is set-based SQL, not a Python per-ticker loop.
        """
        before_ms = slot_start(cutoff_wall_ms)
        previous = self.metadata('finalized_before_ms', 0)
        if before_ms <= previous:
            return 0
        with self.conn:
            self.conn.execute('''INSERT OR IGNORE INTO pending_bars
                (symbol,slot_ms,o,h,l,c,volume,vw)
                WITH ranked AS (
                    SELECT m.*,
                      ROW_NUMBER() OVER(PARTITION BY symbol,slot_ms ORDER BY minute_ms ASC) first_rn,
                      ROW_NUMBER() OVER(PARTITION BY symbol,slot_ms ORDER BY minute_ms DESC) last_rn
                    FROM minute_events m
                    WHERE m.slot_ms < ? AND NOT EXISTS (
                      SELECT 1 FROM invalid_windows bad
                      WHERE m.slot_ms BETWEEN bad.start_ms AND bad.end_ms)
                )
                SELECT symbol,slot_ms,
                  MAX(CASE WHEN first_rn=1 THEN o END),
                  MAX(h), MIN(l),
                  MAX(CASE WHEN last_rn=1 THEN c END),
                  SUM(volume),
                  CASE WHEN SUM(CASE WHEN vw IS NULL THEN 1 ELSE 0 END)=0
                            AND SUM(volume)>0 THEN SUM(volume*vw)*1.0/SUM(volume)
                       ELSE NULL END
                FROM ranked GROUP BY symbol,slot_ms''', (before_ms,))
            inserted = self.conn.execute('SELECT changes()').fetchone()[0]
            self.conn.execute('DELETE FROM minute_events WHERE slot_ms < ?', (before_ms,))
            self.conn.execute('DELETE FROM invalid_windows WHERE end_ms < ?', (before_ms,))
            self.conn.execute('''INSERT INTO metadata(key,value) VALUES('finalized_before_ms',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (before_ms,))
        return inserted

    def pending(self, limit: int = 1500) -> list[tuple]:
        return self.conn.execute('''SELECT symbol,slot_ms,o,h,l,c,volume,vw
            FROM pending_bars ORDER BY slot_ms,symbol LIMIT ?''', (limit,)).fetchall()

    def acknowledge(self, batch: list[tuple]):
        with self.conn:
            self.conn.executemany('DELETE FROM pending_bars WHERE symbol=? AND slot_ms=?',
                                  ((r[0], r[1]) for r in batch))

    def pending_count(self) -> int:
        return self.conn.execute('SELECT COUNT(*) FROM pending_bars').fetchone()[0]


def load_company_map(db: sqlite3.Connection) -> dict[str, tuple[int, ...]]:
    """Existing company ticker map. No company rows or schema are created."""
    out: dict[str, list[int]] = {}
    for company_id, ticker in db.execute(
        "SELECT id,ticker FROM companies WHERE ticker IS NOT NULL AND TRIM(ticker)<>''"
    ):
        out.setdefault(ticker.strip().upper(), []).append(company_id)
    return {k: tuple(v) for k, v in out.items()}


def write_pending(db_path: str | Path, spool: BarSpool,
                  symbols: dict[str, tuple[int, ...]], limit: int = 1500) -> tuple[int, int, int]:
    """Commit SEC DB first, then spool ACK. No overwrite, no migrations.

    If SQLite is locked or a transaction fails, ACK is not performed.
    The spool can therefore safely retry on the next timer/restart.
    """
    batch = spool.pending(limit)
    if not batch:
        return (0, 0, 0)
    unknown = sum(not symbols.get(r[0]) for r in batch)
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    # A per-row iterator is needed for SQLite parameter binding and for the
    # one-to-many ticker->company_id mapping; price aggregation itself is SQL.
    values = [
        (cid, row[1], *row[2:7], row[7], 'massive', 'sip', stamp)
        for row in batch for cid in symbols.get(row[0], ())
    ]
    if values:
        with sqlite3.connect(db_path, timeout=2.0) as db:
            db.execute('PRAGMA busy_timeout=2000')
            # Reuse the existing UNIQUE(company_id,bar_time_utc), CHECK constraints
            # and mobile-summary triggers. Never switch SEC journal mode.
            with db:
                cursor = db.executemany('''INSERT INTO price_bars
                    (company_id,bar_time_utc,open,high,low,close,volume,vwap,
                     source,feed,ingested_at)
                    VALUES (?,strftime('%Y-%m-%dT%H:%M:%SZ', ?/1000, 'unixepoch'),
                            ?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(company_id,bar_time_utc) DO NOTHING''', values)
                inserted = cursor.rowcount
    else:
        inserted = 0
    spool.acknowledge(batch)
    return (len(batch), inserted, unknown)
