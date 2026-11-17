"""Paginated Alpaca 15-minute bar reader with bounded retries and safe errors.

No credential values or full raw HTTP bodies are printed. Sparse symbols with
`bars: null` have no candles; a JSON error/message is NOT treated as empty data.
"""
from __future__ import annotations
from shared.processing_log import log_step
from shared.config import number as config_num, integer as config_int

import time
from datetime import datetime
from urllib.parse import quote

from requests import RequestException, Session

BASE_URL = 'https://data.alpaca.markets/v2/stocks'
MAX_ATTEMPTS = 3


class AlpacaError(RuntimeError):
    pass


def _message(payload):
    """Allow-list short, human-readable error details; never echo a full body."""
    if not isinstance(payload, dict):
        return f'JSON type={type(payload).__name__}'
    keys = ','.join(sorted(str(k) for k in payload.keys())[:8])
    # Error messages don't contain API secrets in normal Alpaca responses,
    # but don't dump the raw response or arbitrary nested values.
    msg = payload.get('message')
    if isinstance(msg, str):
        msg = msg.replace('\r', ' ').replace('\n', ' ')[:160]
        return f'keys=[{keys}], message={msg}'
    return f'keys=[{keys}]'


class AlpacaClient:
    source = "alpaca"
    def __init__(self, key: str, secret: str, *, feed: str = 'sip',
                 min_interval: float | None = None, session=None, sleep=time.sleep,
                 clock=time.monotonic):
        if not key or not secret:
            raise ValueError('Set ALPACA_API_KEY and ALPACA_API_SECRET')
        if feed not in ('sip', 'iex'):
            raise ValueError('feed must be sip or iex')
        self.session = session or Session()
        self.session.headers.update({'APCA-API-KEY-ID': key,
                                     'APCA-API-SECRET-KEY': secret})
        self.feed = feed
        self.min_interval = config_num('ALPACA_MIN_INTERVAL_SECONDS', 0.36, minimum=0.1) if min_interval is None else min_interval
        self.sleep = sleep
        self.clock = clock
        self.next_allowed = 0.0

    def bars(self, symbol: str, start: datetime, end: datetime):
        """Get every page for a single stock; return bars with start <= t < end."""
        params = {'timeframe': '15Min', 'feed': self.feed, 'adjustment': 'raw',
                  'start': start.isoformat().replace('+00:00', 'Z'),
                  'end': end.isoformat().replace('+00:00', 'Z'),
                  'limit': 10000, 'sort': 'asc'}
        output = []
        seen = set()
        url = f'{BASE_URL}/{quote(symbol, safe="")}/bars'
        while True:
            attempts = config_int('ALPACA_MAX_ATTEMPTS',MAX_ATTEMPTS,minimum=1,maximum=10)
            for attempt in range(attempts):
                delay = self.next_allowed - self.clock()
                if delay > 0:
                    self.sleep(delay)
                self.next_allowed = self.clock() + self.min_interval
                log_step('price.http','request',symbol=symbol,feed=self.feed,start=params['start'],end=params['end'],page=len(seen)+1,attempt=attempt+1)
                try:
                    response = self.session.get(url, params=params, timeout=(config_num('ALPACA_CONNECT_TIMEOUT_SECONDS',5,minimum=1),
                                                               config_num('ALPACA_READ_TIMEOUT_SECONDS',20,minimum=1)))
                except RequestException as exc:
                    if attempt == attempts - 1:
                        raise AlpacaError(
                            f'Network timeout/error for {symbol} after {attempts} attempts') from exc
                    self.sleep(2 ** attempt)
                    continue
                status = response.status_code
                log_step('price.http','response',symbol=symbol,status=status,attempt=attempt+1,page=len(seen)+1)
                if status == 429 or 500 <= status < 600:
                    if attempt == attempts - 1:
                        raise AlpacaError(f'Alpaca HTTP {status} for {symbol} after {attempts} attempts')
                    retry = response.headers.get('Retry-After', '')
                    self.sleep(min(float(retry), 30) if retry.isdecimal()
                               else min(2 ** attempt + 1, 15))
                    continue
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise AlpacaError(f'Alpaca HTTP {status}: non-JSON for {symbol}') from exc
                if status in (401, 403):
                    raise AlpacaError(f'Alpaca rejected credentials/feed: HTTP {status} for {symbol}')
                if not response.ok:
                    raise AlpacaError(f'Alpaca HTTP {status} for {symbol} ({_message(payload)})')
                if not isinstance(payload, dict) or 'bars' not in payload:
                    raise AlpacaError(f'Alpaca HTTP {status} invalid bars response for {symbol} '
                                      f'({_message(payload)})')
                if payload['bars'] is None:
                    bars = []
                elif isinstance(payload['bars'], list):
                    bars = payload['bars']
                else:
                    raise AlpacaError(f'Alpaca HTTP {status} invalid bars type for {symbol} '
                                      f'({_message(payload)})')
                break
            # Bar-list validation precedes inserting any data. Any response
            # not a real candle list fails explicitly rather than faking data.
            for bar in bars:  # provider JSON is a list of heterogeneous objects
                if not isinstance(bar, dict):
                    continue
                try:
                    stamp = datetime.fromisoformat(str(bar['t']).replace('Z', '+00:00'))
                except (KeyError, TypeError, ValueError):
                    continue
                if stamp.tzinfo and start <= stamp < end:
                    output.append(bar)
            log_step('price.http','page_parsed',symbol=symbol,received=len(bars),accepted_total=len(output),has_next=bool(payload.get('next_page_token')))
            token = payload.get('next_page_token')
            if not token:
                return output
            if not isinstance(token, str) or token in seen:
                raise AlpacaError(f'Alpaca invalid/repeated page token for {symbol}')
            seen.add(token)
            params['page_token'] = token
