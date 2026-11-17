"""Massive (formerly Polygon) 15-minute US stock aggregates.

Uses authenticated REST requests with bounded rate limiting, retries and
pagination.  Credentials are sent only via Authorization headers, never URLs
or logs.  Bars are raw (split-unadjusted) and converted to the shared Alpaca
bar structure; timestamps are UTC rather than vendor millisecond epochs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from threading import Lock

import pandas as pd
from urllib.parse import quote, urlsplit

from requests import RequestException, Session
from shared.config import integer as config_int, number as config_num
from shared.processing_log import log_step

API_ORIGIN = 'https://api.massive.com'
MAX_PAGES = 100


class MassiveError(RuntimeError):
    pass


class SharedRateLimiter:
    """Shared request-start spacing for all worker sessions, including retries."""

    def __init__(self, interval: float, *, clock=time.monotonic, sleep=time.sleep):
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self._lock = Lock()
        self.next_allowed = 0.0

    def defer(self, seconds: float):
        """Honor one provider cooldown across all threads after an HTTP 429."""
        with self._lock:
            self.next_allowed = max(self.next_allowed, self.clock() + seconds)

    def wait(self):
        with self._lock:
            now = self.clock()
            due = max(now, self.next_allowed)
            self.next_allowed = due + self.interval
        if due > now:
            self.sleep(due - now)


class MassiveClient:
    source = 'massive'
    # Massive's consolidated US equity aggregates correspond to the existing
    # SIP price_bars feed classification.  Keep this compatible with the
    # production CHECK(feed IN ('sip','iex','yfinance')) and its 19 triggers.
    feed = 'sip'

    def __init__(self, key: str, *, session=None, sleep=time.sleep,
                 clock=time.monotonic, min_interval: float | None = None,
                 limiter: SharedRateLimiter | None = None):
        if not key or not key.strip():
            raise ValueError('Set MASSIVE_API_KEY in the private .env file')
        self.session = session or Session()
        self.session.headers.update({
            'Authorization': f'Bearer {key.strip()}',
            'Accept': 'application/json',
        })
        self.sleep = sleep
        self.clock = clock
        # Basic tier allows only 5 req/min; paid plans can override this.
        self.min_interval = (config_num('MASSIVE_MIN_INTERVAL_SECONDS', 12.5, minimum=0)
                             if min_interval is None else float(min_interval))
        if self.min_interval < 0:
            raise ValueError('min_interval must be >= 0')
        self.limiter = limiter or SharedRateLimiter(self.min_interval, clock=clock, sleep=sleep)

    def _fetch(self, symbol: str, url: str, params: dict | None, page: int):
        attempts = config_int('MASSIVE_MAX_ATTEMPTS', 3, minimum=1, maximum=10)
        for attempt in range(attempts):
            self.limiter.wait()
            log_step('price.massive', 'request', symbol=symbol, page=page, attempt=attempt + 1)
            try:
                response = self.session.get(
                    url, params=params,
                    timeout=(config_num('MASSIVE_CONNECT_TIMEOUT_SECONDS', 5, minimum=1),
                             config_num('MASSIVE_READ_TIMEOUT_SECONDS', 30, minimum=1)),
                )
            except RequestException as exc:
                if attempt + 1 == attempts:
                    raise MassiveError(f'Massive network error for {symbol} after {attempts} attempts') from exc
                self.sleep(min(2 ** attempt, 30))
                continue
            code = response.status_code
            log_step('price.massive', 'response', symbol=symbol, page=page, status=code)
            if code in (429, 500, 502, 503, 504):
                if attempt + 1 == attempts:
                    raise MassiveError(f'Massive HTTP {code} for {symbol} after {attempts} attempts')
                retry_after = response.headers.get('Retry-After', '')
                wait = min(float(retry_after), 60) if retry_after.isdecimal() else min(2 ** attempt + 1, 30)
                if code == 429:
                    self.limiter.defer(wait)
                else:
                    self.sleep(wait)
                continue
            if code in (401, 403):
                raise MassiveError(f'Massive credentials/plan rejected: HTTP {code} for {symbol}')
            if not 200 <= code < 300:
                raise MassiveError(f'Massive HTTP {code} for {symbol}')
            try:
                payload = response.json()
            except ValueError as exc:
                raise MassiveError(f'Massive returned non-JSON for {symbol}') from exc
            if not isinstance(payload, dict) or payload.get('status') not in ('OK', 'DELAYED'):
                raise MassiveError(f'Massive returned an invalid/failed status for {symbol}')
            if 'results' not in payload:
                if payload.get('resultsCount') == 0:
                    payload['results'] = []
                else:
                    raise MassiveError(f'Massive response missing results for {symbol}')
            if not isinstance(payload['results'], list):
                raise MassiveError(f'Massive response results are not a list for {symbol}')
            count = payload.get('resultsCount')
            if count is not None and (type(count) is not int or
                                      count != len(payload['results'])):
                raise MassiveError(f'Massive response result count mismatch for {symbol}')
            return payload
        raise AssertionError('unreachable')

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        """Return every bar with start <= timestamp < end; never fabricate prices."""
        if (start.tzinfo is None or start.utcoffset() is None or
                end.tzinfo is None or end.utcoffset() is None):
            raise ValueError('Massive start/end must be timezone-aware')
        if start >= end:
            raise ValueError('start must be before end')
        sym = symbol.strip().upper()
        if not sym:
            raise ValueError('symbol must not be empty')
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        # Massive builds multiplier-minute bars from the snapped query start.
        # With a rolling --last window whose first minute is arbitrary, a 15m
        # request can otherwise yield bars at e.g. :07/:22/:37/:52, which the
        # shared canonical UTC quarter-hour validator must reject. Anchor the
        # vendor request to a stable UTC 15-minute boundary, then retain the
        # exact user-requested [start, end) filtering below. Never retimestamp
        # or relabel OHLC data to force it onto the grid.
        aligned_start = start.replace(minute=(start.minute // 15) * 15,
                                      second=0, microsecond=0)
        start_ms = int(aligned_start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        path = f'/v2/aggs/ticker/{quote(sym, safe="")}/range/15/minute/{start_ms}/{end_ms}'
        url = API_ORIGIN + path
        params = {'adjusted': 'false', 'sort': 'asc', 'limit': 50000}
        output: list[dict] = []
        seen_urls = set()
        last_tick = None
        for page in range(1, MAX_PAGES + 1):
            payload = self._fetch(sym, url, params, page)
            items = payload['results']
            if items:
                # Provider envelope/record shape checks cannot be columnar, but
                # timestamp conversion, time filtering and bar projection are.
                if not all(isinstance(item, dict) for item in items):
                    raise MassiveError(f'Massive returned a non-object bar for {sym}')
                frame = pd.DataFrame.from_records(items)
                required = {'t', 'o', 'h', 'l', 'c'}
                if not required.issubset(frame.columns):
                    raise MassiveError(f'Massive returned invalid OHLC data for {sym}')
                ticks = frame['t']
                if ticks.dtype.kind not in ('i', 'u'):
                    raise MassiveError(f'Massive returned invalid OHLC data for {sym}')
                if last_tick is not None and ticks.iloc[0] < last_tick:
                    raise MassiveError(f'Massive returned out-of-order pages for {sym}')
                if not ticks.is_monotonic_increasing:
                    raise MassiveError(f'Massive returned unsorted candles for {sym}')
                last_tick = int(ticks.iloc[-1])
                instants = pd.to_datetime(ticks, unit='ms', utc=True, errors='coerce')
                if instants.isna().any():
                    raise MassiveError(f'Massive returned invalid OHLC data for {sym}')
                valid = instants.ge(pd.Timestamp(start)) & instants.lt(pd.Timestamp(end))
                data = frame.loc[valid, ['o', 'h', 'l', 'c']].copy()
                # Price-only ingest: absent volume is optional metadata, not
                # grounds for discarding an otherwise valid OHLC candle.
                data['v'] = frame.loc[valid, 'v'] if 'v' in frame else None
                data.insert(0, 't', instants.loc[valid].dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
                for col in ('n', 'vw'):
                    data[col] = frame.loc[valid, col] if col in frame else None
                # pandas may represent absent vendor values as NaN; restore JSON null.
                data = data.astype(object).where(pd.notna(data), None)
                output.extend(data.to_dict('records'))
            next_url = payload.get('next_url')
            if not next_url:
                # Massive can limit base minute bars before aggregating; a
                # result at that cap without a cursor is incomplete.
                if payload.get('queryCount', 0) >= 50000:
                    raise MassiveError(f'Massive aggregate query limit reached without pagination for {sym}; reduce --last')
                return output
            if not isinstance(next_url, str):
                raise MassiveError(f'Massive returned invalid pagination for {sym}')
            try:
                parsed = urlsplit(next_url)
                safe_url = (parsed.scheme == 'https' and parsed.hostname == 'api.massive.com'
                            and parsed.port in (None, 443) and parsed.username is None
                            and parsed.password is None and parsed.path == path)
            except ValueError:
                safe_url = False
            if not safe_url:
                raise MassiveError(f'Massive returned unsafe pagination for {sym}')
            if next_url in seen_urls:
                raise MassiveError(f'Massive repeated pagination cursor for {sym}')
            seen_urls.add(next_url)
            url, params = next_url, None
        raise MassiveError(f'Massive exceeded {MAX_PAGES} result pages for {sym}')
