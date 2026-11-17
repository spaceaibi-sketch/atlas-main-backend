"""Standalone Massive WebSocket stocks listener; never touches Atlas REST API.

AM.* provides official, condition-filtered per-minute trade aggregates for all
stock symbols. This daemon rolls those *observed* minutes into UTC-aligned 15m
bars. It does not fabricate missing minutes or promise uninterrupted feed
coverage. A dedicated durable SQLite spool survives main-DB writer contention.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

from sec_edgar.windows_launcher import load_env
from .massive_ws_store import BarSpool, parse_minute, slot_start, write_pending, load_company_map

UTC = timezone.utc
WS_URLS = {
    'delayed': 'wss://delayed.massive.com/stocks',
    'realtime': 'wss://socket.massive.com/stocks',
}
PERIOD_MS = 900_000


def log(message: str):
    stamp = datetime.now(UTC).isoformat(timespec='seconds')
    print(f'{stamp} [MASSIVE WS] {message}', flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


class RemoteStatusError(RuntimeError):
    pass


def scan_status(frame: str | bytes) -> tuple[list[dict], list[dict]]:
    try:
        data = json.loads(frame)
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid Massive JSON frame') from exc
    if not isinstance(data, list):
        raise ValueError('Unexpected non-array Massive WebSocket frame')
    statuses = []
    events = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get('ev') == 'status':
            statuses.append(item)
        elif item.get('ev') == 'AM':
            events.append(item)
    for item in statuses:
        status = str(item.get('status', '')).lower()
        if status in ('auth_failed', 'error', 'failed', 'not_authorized') or (
                'not authorized' in str(item.get('message', '')).lower()):
            raise RemoteStatusError(f'Massive rejected WebSocket request: {status}')
    return statuses, events


def authenticate_and_subscribe(ws):
    """Wait for auth_success before subscribing, per Massive's protocol."""
    from websocket import WebSocketTimeoutException
    start = time.monotonic()
    ws.send(json.dumps({'action': 'auth', 'params': os.environ['MASSIVE_API_KEY']}))
    authorized = False
    while time.monotonic() - start < 25:
        try:
            statuses, _ = scan_status(ws.recv())
        except WebSocketTimeoutException:
            continue
        if any(s.get('status') == 'auth_success' for s in statuses):
            authorized = True
            break
    if not authorized:
        raise RemoteStatusError('No auth_success from Massive in 25 seconds')
    ws.send(json.dumps({'action': 'subscribe', 'params': 'AM.*'}))
    start = time.monotonic()
    while time.monotonic() - start < 25:
        try:
            statuses, _ = scan_status(ws.recv())
        except WebSocketTimeoutException:
            continue
        if any(s.get('status') == 'success' and 'AM.*' in str(s.get('message', ''))
               for s in statuses):
            return
    raise RemoteStatusError('No AM.* subscription confirmation from Massive')


def positive(value: str) -> float:
    v = float(value)
    if not (0 <= v < 3600):
        raise argparse.ArgumentTypeError('Must be between 0 and 3600 seconds')
    return v


def arguments(argv=None):
    load_env()
    p = argparse.ArgumentParser(description='Standalone Massive AM.* 15-minute bar service')
    p.add_argument('--feed', choices=('delayed', 'realtime'),
                   default=os.environ.get('MASSIVE_WS_FEED', 'delayed'))
    p.add_argument('--db', default=os.environ.get('SEC_DB_PATH', '/opt/atlas/data/edgar.sqlite3'))
    p.add_argument('--spool', default=os.environ.get('MASSIVE_WS_SPOOL_PATH',
                        '/opt/atlas/data/edgar.sqlite3.massive_ws_spool.sqlite3'))
    p.add_argument('--grace-seconds', type=positive,
                   default=float(os.environ.get('MASSIVE_WS_GRACE_SECONDS', '90')))
    p.add_argument('--max-queued-events', type=int, default=10000)
    args = p.parse_args(argv)
    if args.max_queued_events < 10:
        p.error('--max-queued-events must be >= 10')
    if not os.environ.get('MASSIVE_API_KEY', '').strip():
        p.error('MASSIVE_API_KEY must be set in existing private .env')
    if not Path(args.db).is_file():
        p.error(f'Existing SEC database does not exist: {args.db}')
    return args


def run_once(args, spool: BarSpool, stopped) -> None:
    """Read one connection; reconnect after socket drops; no REST calls."""
    from websocket import WebSocketTimeoutException, create_connection

    delay_ms = 900_000 if args.feed == 'delayed' else 0
    grace_ms = int(args.grace_seconds * 1000)
    # Worst-case interruption includes the partially received slot at both
    # sides of a disconnect; none of those intervals may be called complete.
    last_seen_wall = spool.metadata('last_connected_wall_ms', 0)
    current_wall = now_ms()
    first_impacted = last_seen_wall - delay_ms if last_seen_wall else current_wall - delay_ms
    spool.invalidate(first_impacted, current_wall - delay_ms)
    ws = None
    staged = []
    last_flush = time.monotonic()
    last_metrics = last_flush
    previous_mapping = 0.0
    last_maintenance = 0.0
    mapping = {}
    errors = 0
    received = 0
    invalid = 0
    inserted = 0
    try:
        ws = create_connection(WS_URLS[args.feed], timeout=5, enable_multithread=True)
        ws.settimeout(1.0)
        authenticate_and_subscribe(ws)
        log(f'subscribed AM.* feed={args.feed}; live_db={args.db}; spool={args.spool}')
        while not stopped():
            try:
                raw = ws.recv()
                if not raw:
                    raise ConnectionError('empty/closed WebSocket frame')
                statuses, events = scan_status(raw)
                for status in statuses:
                    if status.get('status') not in ('success', 'connected', 'auth_success'):
                        log(f'status={status.get("status")}')
                received += len(events)
                for e in events:
                    row = parse_minute(e)
                    if row is None:
                        invalid += 1
                    else:
                        staged.append(row)
            except WebSocketTimeoutException:
                pass
            moment = time.monotonic()
            if staged and (len(staged) >= args.max_queued_events or moment-last_flush >= 1.0):
                spool.stage(staged)
                staged.clear()
                last_flush = moment
            if moment-last_flush >= 1.0:
                last_flush = moment
            if moment - last_maintenance < 1.0:
                continue
            last_maintenance = moment
            # A short database busy timeout avoids monopolizing the one SQLite
            # writer. On BUSY we keep finalized pending rows in the spool.
            if moment-previous_mapping >= 15 * 60 or not mapping:
                try:
                    with sqlite3.connect(f'file:{Path(args.db).resolve()}?mode=ro',
                                         uri=True, timeout=2.0) as db:
                        mapping = load_company_map(db)
                    previous_mapping = moment
                except sqlite3.Error as exc:
                    log(f'cannot refresh company mapping; retrying: {exc}')
                    previous_mapping = moment - 14 * 60
            # Do not close the delayed 15m window until the provider delay +
            # explicit finalization grace have elapsed.
            spool.close_completed(now_ms() - delay_ms - grace_ms)
            if mapping and spool.pending_count():
                try:
                    done, saved, unknown = write_pending(args.db, spool, mapping)
                    if done:
                        inserted += saved
                        log(f'committed pending={done} inserted={saved} unknown_symbols={unknown} '
                            f'remaining={spool.pending_count()}')
                except sqlite3.Error as exc:
                    errors += 1
                    if errors == 1 or errors % 30 == 0:
                        log(f'live database busy/unavailable, keeping pending bars: {exc}')
            spool.record_clock(now_ms())
            if moment-last_metrics > 60:
                log(f'heartbeat messages={received} rejected={invalid} inserted={inserted} '
                    f'pending={spool.pending_count()}')
                last_metrics = moment
    finally:
        if staged:
            spool.stage(staged)
        # Any slot that may be missing stream updates during reconnect is unsafe.
        spool.invalidate(spool.metadata('last_connected_wall_ms', now_ms()) - delay_ms,
                         now_ms() - delay_ms)
        if ws is not None:
            ws.close()


def main(argv=None) -> int:
    args = arguments(argv)
    try:
        import websocket  # noqa: F401
    except ImportError:
        log('missing dependency websocket-client; install requirements-massive-ws.txt')
        return 2
    stop = False
    def stop_signal(_signum, _frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    log(f'starting all-symbol AM.* listener feed={args.feed} (no REST API changes)')
    spool = BarSpool(args.spool)
    retries = 0
    try:
        while not stop:
            try:
                run_once(args, spool, lambda: stop)
                retries = 0
            except (OSError, ConnectionError, TimeoutError, ValueError, RemoteStatusError) as exc:
                retries += 1
                log(f'connection interrupted: {type(exc).__name__}: {exc}')
            except Exception as exc:
                retries += 1
                log(f'error; reconnecting without losing spool: {type(exc).__name__}: {exc}')
            if not stop:
                backoff = min(60, 2 ** min(retries, 6))
                for _ in range(backoff):
                    if stop:
                        break
                    time.sleep(1)
    finally:
        spool.close()
    log('stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
