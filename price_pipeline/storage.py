"""Price bars live in the shared SEC database, identified by companies.id."""
from __future__ import annotations
from shared.processing_log import log_step

import csv
import numpy as np
import pandas as pd
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc

PRICE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS price_bars (
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  bar_time_utc TEXT NOT NULL CHECK(bar_time_utc GLOB '????-??-??T??:??:??Z'),
  open REAL NOT NULL CHECK(open > 0),
  high REAL NOT NULL CHECK(high > 0),
  low REAL NOT NULL CHECK(low > 0),
  close REAL NOT NULL CHECK(close > 0),
  volume INTEGER NOT NULL CHECK(volume >= 0),
  trade_count INTEGER CHECK(trade_count >= 0),
  vwap REAL,
  source TEXT NOT NULL DEFAULT 'alpaca',
  feed TEXT NOT NULL CHECK(feed IN ('sip', 'iex', 'yfinance')),
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (company_id, bar_time_utc)
);
CREATE INDEX IF NOT EXISTS idx_price_bars_recent ON price_bars(bar_time_utc DESC);
'''




def ensure_yfinance_feed(conn):
    """Back up and widen the historical price_bars feed constraint once.

    The original live table only permitted sip/iex. Its check constraint cannot
    be altered in place by SQLite. This exact-schema copy preserves existing
    values and keys; no price records are rewritten by the provider itself.
    """
    raw = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='price_bars'").fetchone()
    if raw is None:
        raise RuntimeError('Missing price_bars table')
    if "'yfinance'" in raw[0]:
        return False
    columns = {r[1] for r in conn.execute('PRAGMA table_info(price_bars)')}
    required = {'company_id', 'bar_time_utc', 'open', 'high', 'low', 'close',
                'volume', 'trade_count', 'vwap', 'source', 'feed', 'ingested_at'}
    if columns != required or 'CHECK(feed IN' not in raw[0]:
        raise RuntimeError('Unknown price_bars schema; refusing automatic migration')
    extras = conn.execute("SELECT type,name FROM sqlite_master WHERE tbl_name='price_bars' "
                          "AND (type='trigger' OR (type='index' AND name NOT LIKE 'sqlite_autoindex%'))").fetchall()
    known_indexes = {'idx_price_bars_recent', 'idx_atlas_mob_price_at'}
    # Retain the mobile-summary triggers installed by our own migration when
    # rebuilding the older price_bars constraint. Unknown custom objects still
    # block this destructive table replacement.
    known_triggers = {'atlas_mob_price_ai', 'atlas_mob_price_ad', 'atlas_mob_price_au'}
    if any((kind == 'index' and name not in known_indexes) or
           (kind == 'trigger' and name not in known_triggers) for kind, name in extras):
        raise RuntimeError('Custom price_bars indexes/triggers detected; refusing automatic migration')
    # An AFTER INSERT trigger on summary also embeds a price_bars
    # lookup. SQLite refuses RENAME of the rebuilt table while that dependent
    # trigger points to a temporarily missing table; save and restore it too.
    dependent_triggers = list(conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE type='trigger' "
        "AND sql LIKE '%price_bars%' AND sql IS NOT NULL"))
    if any(name not in known_triggers | {'atlas_mob_summary_ai'}
           for _kind,name,_sql in dependent_triggers):
        raise RuntimeError('Unknown trigger depends on price_bars; refusing automatic migration')
    saved_objects = list(conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='price_bars' "
        "AND name='idx_atlas_mob_price_at' AND sql IS NOT NULL")) + dependent_triggers
    db_path = conn.execute('PRAGMA database_list').fetchone()[2]
    if not db_path:
        raise RuntimeError('Cannot safely back up in-memory database for migration')
    from sec_edgar.database import backup_database
    path = Path(db_path)
    backup = path.with_name(path.name + '.before_yfinance_feed_v1.bak')
    if not backup.exists():
        backup_database(conn, backup)
    was_fk = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    conn.execute('PRAGMA foreign_keys=OFF')
    definition = PRICE_SCHEMA.split(';')[0].replace('IF NOT EXISTS price_bars', 'price_bars_yfinance_v1')
    fields = ','.join(('company_id', 'bar_time_utc', 'open', 'high', 'low', 'close',
                       'volume', 'trade_count', 'vwap', 'source', 'feed', 'ingested_at'))
    try:
        conn.execute('BEGIN IMMEDIATE')
        for _kind, name, _ddl in dependent_triggers:
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(definition)
        conn.execute(f'INSERT INTO price_bars_yfinance_v1({fields}) SELECT {fields} FROM price_bars')
        conn.execute('DROP TABLE price_bars')
        conn.execute('ALTER TABLE price_bars_yfinance_v1 RENAME TO price_bars')
        conn.execute('CREATE INDEX idx_price_bars_recent ON price_bars(bar_time_utc DESC)')
        for _kind, _name, ddl in saved_objects:
            conn.execute(ddl)
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Foreign-key check failed; migration rolled back')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f'PRAGMA foreign_keys={int(bool(was_fk))}')
    return True

def normalize_bars(bars: list[dict], start: datetime, end: datetime,
                   *, diagnostics: dict | None = None,
                   allow_fractional_volume: bool = False,
                   price_only: bool = False):
    """Vectorized price validation with source-specific optional market metadata.

    For Massive, price_only makes OHLC and aligned UTC timestamps authoritative.
    Missing/invalid volume becomes zero solely to satisfy the legacy NOT NULL
    column; missing/invalid trade counts and VWAP become NULL. No price is
    synthesized, modified, or accepted with invalid OHLC/time. Other sources
    retain their existing strict volume/count/VWAP rules.
    """
    if diagnostics is not None:
        diagnostics['received'] = len(bars)
        diagnostics['rejection_reasons'] = {}
        diagnostics['duplicates_in_response'] = 0
        diagnostics['valid_unique'] = 0
        diagnostics['rejected_invalid'] = 0
        diagnostics['volume_defaulted'] = 0
        diagnostics['trade_count_ignored'] = 0
        diagnostics['vwap_ignored'] = 0
        diagnostics['price_only'] = price_only
        diagnostics['first_valid_utc'] = None
        diagnostics['last_valid_utc'] = None
    if not bars:
        return pd.DataFrame(columns=['t', 'o', 'h', 'l', 'c', 'v', 'n', 'vw']), 0
    objects = [bar for bar in bars if isinstance(bar, dict)]
    if diagnostics is not None:
        diagnostics['rejection_reasons']['non_object'] = len(bars) - len(objects)
    if not objects:
        if diagnostics is not None:
            diagnostics['rejected_invalid'] = len(bars)
        return pd.DataFrame(columns=['t', 'o', 'h', 'l', 'c', 'v', 'n', 'vw']), len(bars)
    # JSON shape/null detection is the only per-record step; arithmetic stays vectorized.
    optional_null = pd.DataFrame({col: [col not in b or b[col] is None for b in objects]
                                  for col in ('n', 'vw')})
    frame = pd.DataFrame.from_records(objects).reindex(columns=['t', 'o', 'h', 'l', 'c', 'v', 'n', 'vw'])
    raw_t = frame['t'].astype('string')
    explicit_tz = raw_t.str.contains(r'(?:Z|[+-]\d\d:\d\d)$', case=False, na=False)
    # format=mixed recognizes valid ISO offsets but never supplies a timezone for us.
    instant = pd.to_datetime(raw_t.where(explicit_tz), utc=True, errors='coerce', format='mixed')
    start = pd.Timestamp(start.astimezone(UTC))
    end = pd.Timestamp(end.astimezone(UTC))
    in_window = instant.ge(start) & instant.lt(end)
    # The timestamps must identify UTC 15-minute interval STARTS.
    on_grid = (instant.dt.minute.mod(15).eq(0) & instant.dt.second.eq(0)
               & instant.dt.microsecond.eq(0) & instant.dt.nanosecond.eq(0))
    valid = instant.notna() & in_window & on_grid
    numeric = frame[['o', 'h', 'l', 'c', 'v', 'n', 'vw']].apply(pd.to_numeric, errors='coerce')
    required = numeric[['o', 'h', 'l', 'c']] if price_only else numeric[['o', 'h', 'l', 'c', 'v']]
    finite_required = pd.Series(np.isfinite(required.to_numpy(dtype='float64')).all(axis=1),
                                index=frame.index)
    op, high, low, close = (numeric[k] for k in ('o', 'h', 'l', 'c'))
    valid_ohlc = (finite_required & low.gt(0) & low.le(op) & low.le(close)
                  & high.ge(op) & high.ge(close))
    valid &= valid_ohlc
    # Optional fields must not discard otherwise valid Massive OHLC bars.
    # Other providers retain strict nonnegative finite count requirements.
    counts_valid = {}
    for col in ('v', 'n'):
        x = numeric[col]
        optional = col == 'n'
        allowed = x.notna() & np.isfinite(x) & x.ge(0) & x.lt(2**63)
        if not (col == 'v' and allow_fractional_volume):
            allowed &= x.mod(1).eq(0)
        if optional:
            allowed |= x.isna() & optional_null[col]
        counts_valid[col] = allowed
        if not price_only:
            valid &= allowed
    valid_vwap = ((numeric['vw'].isna() & optional_null['vw']) |
                  (np.isfinite(numeric['vw']) & numeric['vw'].gt(0)))
    if not price_only:
        valid &= valid_vwap
    if diagnostics is not None:
        # Each input row is attributed to its FIRST failed check. These masks
        # are vectorized, mutually exclusive, and sum to all invalid records.
        remaining = pd.Series(True, index=frame.index)
        ignored_optional = pd.Series(False, index=frame.index)
        failure_checks = (
            ('timestamp_missing_timezone', ~explicit_tz),
            ('timestamp_unparseable', instant.isna()),
            ('timestamp_outside_window', ~in_window),
            ('timestamp_not_15m', ~on_grid),
            ('invalid_ohlc', ~valid_ohlc),
            ('invalid_volume', ~counts_valid['v'] if not price_only else ignored_optional),
            ('invalid_trade_count', ~counts_valid['n'] if not price_only else ignored_optional),
            ('invalid_vwap', ~valid_vwap if not price_only else ignored_optional),
        )
        for reason, failed in failure_checks:
            attributed = remaining & failed.fillna(True)
            diagnostics['rejection_reasons'][reason] = int(attributed.sum())
            remaining &= ~attributed
    result = numeric.loc[valid].copy()
    result.insert(0, 't', instant.loc[valid].dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
    # Provider pagination may overlap. Stable first occurrence wins, always.
    before_dedup = len(result)
    result = result.drop_duplicates('t', keep='first').sort_values('t', kind='stable')
    if diagnostics is not None:
        diagnostics['duplicates_in_response'] = before_dedup - len(result)
        diagnostics['valid_unique'] = len(result)
        diagnostics['rejected_invalid'] = len(bars) - before_dedup
        diagnostics['first_valid_utc'] = result['t'].iloc[0] if len(result) else None
        diagnostics['last_valid_utc'] = result['t'].iloc[-1] if len(result) else None
    if len(result):
        if price_only:
            # Only update optional metadata on rows that have ALREADY passed
            # strict UTC-time and OHLC validation. No rowwise Python processing.
            # Existing schema: volume INTEGER NOT NULL CHECK(volume >= 0).
            # 0 denotes unavailable volume, NOT a measured zero-share bar.
            bad_volume = ~counts_valid['v'].loc[result.index]
            bad_count = ~counts_valid['n'].loc[result.index]
            bad_vwap = ~valid_vwap.loc[result.index]
            result.loc[bad_volume, 'v'] = 0.0
            result.loc[bad_count, 'n'] = np.nan
            result.loc[bad_vwap, 'vw'] = np.nan
            if diagnostics is not None:
                diagnostics['volume_defaulted'] = int(bad_volume.sum())
                diagnostics['trade_count_ignored'] = int(bad_count.sum())
                diagnostics['vwap_ignored'] = int(bad_vwap.sum())
        # Preserve valid Massive fractional-share volume without rounding.
        if not allow_fractional_volume:
            result['v'] = result['v'].astype('int64')
        result['n'] = result['n'].astype('Int64')
    rejected = len(bars) - len(result)
    return result, rejected


def ingest_bars(conn, company_id: int, bars: list[dict], start: datetime,
                end: datetime, feed: str, *, now: datetime, source: str = 'alpaca',
                diagnostics: dict | None = None) -> int:
    """Merge verified bars with one set-based INSERT SELECT, without overwrites."""
    if (source, feed) not in (('alpaca', 'sip'), ('alpaca', 'iex'),
                              ('yfinance', 'yfinance'), ('massive', 'sip')):
        raise ValueError('Invalid price source/feed combination')
    frame, rejected = normalize_bars(
        bars, start, end, diagnostics=diagnostics,
        allow_fractional_volume=(source == 'massive'),
        price_only=(source == 'massive'))
    stamp = now.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    # Stage into TEMP: keep one atomic write and let existing INSERT triggers fire.
    # executemany is used only to transfer a columnar array into SQLite; the
    # actual merge is a single SQL statement and never reads or overwrites a key.
    data = frame[['t', 'o', 'h', 'l', 'c', 'v', 'n', 'vw']].astype(object).where(pd.notna(frame), None)
    values = [tuple(r) for r in data.itertuples(index=False, name=None)]
    with conn:
        conn.execute("""CREATE TEMP TABLE IF NOT EXISTS atlas_price_stage (
            bar_time_utc TEXT PRIMARY KEY, open REAL, high REAL, low REAL,
            close REAL, volume INTEGER, trade_count INTEGER, vwap REAL)""")
        conn.execute('DELETE FROM atlas_price_stage')
        if values:
            conn.executemany('INSERT INTO atlas_price_stage VALUES (?,?,?,?,?,?,?,?)', values)
        if diagnostics is not None:
            # Exact primary-key matches BEFORE the merge; no guesses based on
            # INSERT rowcount and no modifications to existing records.
            existing = conn.execute("""SELECT p.source, p.feed, COUNT(*)
                FROM atlas_price_stage s JOIN price_bars p
                  ON p.company_id=? AND p.bar_time_utc=s.bar_time_utc
                GROUP BY p.source, p.feed ORDER BY p.source, p.feed""",
                (company_id,)).fetchall()
            diagnostics['existing_by_source_feed'] = {
                f'{r[0]}/{r[1]}': int(r[2]) for r in existing
            }
            diagnostics['existing_keys'] = sum(int(r[2]) for r in existing)
            diagnostics['fresh_candidates'] = len(frame) - diagnostics['existing_keys']
            before = conn.execute("""SELECT COUNT(*), MIN(bar_time_utc), MAX(bar_time_utc)
                FROM price_bars WHERE company_id=?""", (company_id,)).fetchone()
            diagnostics['company_rows_before'] = before[0]
            diagnostics['db_first_utc'] = before[1]
            diagnostics['db_last_utc'] = before[2]
        cursor = conn.execute("""INSERT INTO price_bars
            (company_id,bar_time_utc,open,high,low,close,volume,trade_count,
             vwap,source,feed,ingested_at)
            SELECT ?,bar_time_utc,open,high,low,close,volume,trade_count,
                   vwap,?,?,? FROM atlas_price_stage WHERE 1=1
            ON CONFLICT(company_id,bar_time_utc) DO NOTHING""",
            (company_id, source, feed, stamp))
        saved = cursor.rowcount
        if diagnostics is not None:
            diagnostics['sqlite_changes'] = conn.execute('SELECT changes()').fetchone()[0]
            after = conn.execute("""SELECT COUNT(*) FROM price_bars
                WHERE company_id=?""", (company_id,)).fetchone()[0]
            diagnostics['company_rows_after'] = after
            diagnostics['missing_after_merge'] = conn.execute("""SELECT COUNT(*)
                FROM atlas_price_stage s LEFT JOIN price_bars p
                  ON p.company_id=? AND p.bar_time_utc=s.bar_time_utc
                WHERE p.company_id IS NULL""", (company_id,)).fetchone()[0]
            diagnostics['saved'] = saved
            diagnostics['merge_mismatch'] = bool(
                diagnostics['fresh_candidates'] != saved or
                diagnostics['sqlite_changes'] != saved or
                diagnostics['company_rows_after'] - diagnostics['company_rows_before'] != saved or
                diagnostics['missing_after_merge']
            )
        conn.execute('DELETE FROM atlas_price_stage')
    log_step('price.bar', 'batch_completed', company_id=company_id,
             received=len(bars), valid=len(frame), rejected=rejected,
             saved=saved, duplicate_or_existing=len(frame)-saved)
    if diagnostics is not None:
        log_step('price.bar', 'merge_diagnostics', company_id=company_id,
                 source=source, feed=feed, **diagnostics,
                 level='error' if diagnostics['merge_mismatch'] else 'info')
    return saved


def missing_regular_slots(conn, company_id: int, start: datetime, end: datetime,
                          *, bounded_by_data: bool = True) -> list[datetime]:
    """Identify absent XNYS slot starts in UTC, using a set-based SQL read.

    Only inspect sessions with at least two observed bars, and only between
    their earliest/latest times, by default. This avoids calling non-trading
    pre/post-session periods or wholly empty symbols gaps. Sparse trades and
    halts may still be legitimate, so absence is not proof of a provider error.
    """
    from .calendar import session_slots
    slots = session_slots(start, end)
    if not slots:
        return []
    expected = set().union(*slots.values())
    rows = conn.execute("""SELECT bar_time_utc FROM price_bars
        WHERE company_id=? AND bar_time_utc>=? AND bar_time_utc<?
        ORDER BY bar_time_utc""",
        (company_id, start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
         end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'))).fetchall()
    actual = {row[0] for row in rows}
    if bounded_by_data:
        from collections import defaultdict
        observed = defaultdict(list)
        for t in actual & expected:
            # A market session can straddle UTC dates for some exchanges;
            # XNYS sessions do not, so UTC date is its session key here.
            observed[t[:10]].append(t)
        expected = {t for date, group in slots.items()
                    if len(observed[date]) >= 2
                    for t in group if min(observed[date]) < t < max(observed[date])}
    return [datetime.fromisoformat(t.replace('Z', '+00:00')) for t in sorted(expected - actual)]


def export_company_csv(conn, company_id, symbol, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    # Use id to avoid path collisions/symbol path injection and keep stable identity.
    dest = output_dir / f'company_{int(company_id)}_15min.csv'
    with dest.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['company_id','symbol','datetime_utc','open','high','low',
                         'close','volume','trade_count','vwap','feed','source'])
        cursor = conn.execute('''SELECT bar_time_utc,open,high,low,close,volume,
             trade_count,vwap,feed,source FROM price_bars WHERE company_id=?
             ORDER BY bar_time_utc''', (company_id,))
        writer.writerows((company_id,symbol,*tuple(row)) for row in cursor)
    return dest
