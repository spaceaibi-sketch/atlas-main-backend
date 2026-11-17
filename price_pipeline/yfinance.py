"""Yahoo Finance 15-minute regular-session raw OHLCV, via yfinance.

Yahoo intraday prices are limited to approximately 60 days. Returns the same
Alpaca-style dict shape consumed by shared price validation/storage.
"""
from __future__ import annotations

from datetime import datetime, timezone

from shared.config import number as config_num


class YFinanceError(RuntimeError):
    pass


class YFinanceClient:
    source = 'yfinance'
    feed = 'yfinance'

    def __init__(self, *, ticker_factory=None):
        if ticker_factory is None:
            try:
                import yfinance as yf
            except ImportError as exc:
                raise YFinanceError('yfinance is not installed; pip install -r requirements.txt') from exc
            ticker_factory = yf.Ticker
        self.ticker_factory = ticker_factory

    def bars(self, symbol: str, start: datetime, end: datetime):
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError('yfinance start/end must be timezone-aware')
        try:
            import pandas as pd
            frame = self.ticker_factory(symbol).history(
                start=start.astimezone(timezone.utc),
                end=end.astimezone(timezone.utc),
                interval='15m', prepost=False, auto_adjust=False,
                actions=False, repair=False,
                timeout=config_num('YFINANCE_TIMEOUT_SECONDS', 20, minimum=1),
                raise_errors=True,
            )
            if frame is None or frame.empty:
                return []
            if isinstance(frame.columns, pd.MultiIndex):
                raise YFinanceError(f'Unexpected multiple-ticker columns for {symbol}')
            required = ('Open', 'High', 'Low', 'Close', 'Volume')
            if not set(required).issubset(frame.columns):
                raise YFinanceError(f'Missing Yahoo OHLCV columns for {symbol}')
            if getattr(frame.index, 'tz', None) is None:
                raise YFinanceError(f'Yahoo timestamps lack a timezone for {symbol}')
            timestamps = pd.to_datetime(frame.index, utc=True, errors='coerce')
            clean = pd.DataFrame({
                't': timestamps.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'o': pd.to_numeric(frame['Open'].to_numpy(), errors='coerce'),
                'h': pd.to_numeric(frame['High'].to_numpy(), errors='coerce'),
                'l': pd.to_numeric(frame['Low'].to_numpy(), errors='coerce'),
                'c': pd.to_numeric(frame['Close'].to_numpy(), errors='coerce'),
                'v': pd.to_numeric(frame['Volume'].to_numpy(), errors='coerce'),
            })
            # Do not synthesize missing volume, price, or timestamps.
            clean = clean.dropna(subset=['t', 'o', 'h', 'l', 'c', 'v'])
            clean = clean.loc[clean['v'].ge(0) & clean['v'].mod(1).eq(0)]
            clean['v'] = clean['v'].astype('int64')
            clean['n'] = None  # Yahoo's standard history does not supply trade count.
            clean['vw'] = None  # Nor an Alpaca-compatible VWAP.
            return clean.to_dict(orient='records')
        except YFinanceError:
            raise
        except Exception as exc:
            # Avoid leaking raw exception URLs/request headers into the CLI log.
            raise YFinanceError(f'Yahoo 15-minute download failed for {symbol}: '
                                f'{type(exc).__name__}') from exc
