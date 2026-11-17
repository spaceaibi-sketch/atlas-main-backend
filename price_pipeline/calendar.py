"""Authoritative XNYS session boundaries, including holidays, DST and early closes.

Use an exchange calendar instead of assuming every weekday has 26 intervals.
A bar's timestamp identifies the START of a 15-minute interval in UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

UTC = timezone.utc
STEP = timedelta(minutes=15)


@lru_cache(maxsize=64)
def session_slots(start: datetime, end: datetime) -> dict[str, set[str]]:
    """Return cached expected regular-session bar starts in [start, end).

    Each 15-minute bar must also have closed by `end`. Callers enforce an
    additional provider publication lag (15 minutes for delayed SIP access).
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError('Calendar boundaries must be timezone-aware')
    if end <= start:
        return {}
    calendar = xcals.get_calendar('XNYS')
    begin_date = pd.Timestamp(start.astimezone(UTC).date()) - pd.Timedelta(days=1)
    end_date = pd.Timestamp(end.astimezone(UTC).date()) + pd.Timedelta(days=1)
    sessions = calendar.sessions_in_range(begin_date, end_date)
    result = {}
    for session in sessions:  # Session calendars are irregular (half-days/DST).
        market_open = calendar.session_open(session).to_pydatetime()
        market_close = calendar.session_close(session).to_pydatetime()
        first = max(start, market_open)
        last = min(end - STEP, market_close - STEP)
        if last < first:
            continue
        starts = pd.date_range(market_open, market_close - STEP,
                               freq='15min', tz='UTC')
        filtered = starts[(starts >= pd.Timestamp(first)) &
                          (starts <= pd.Timestamp(last))]
        if len(filtered):
            result[session.strftime('%Y-%m-%d')] = {
                t.strftime('%Y-%m-%dT%H:%M:%SZ') for t in filtered
            }
    return result


def safe_end(now: datetime) -> datetime:
    """Exclude bars still forming and the free SIP 15-minute publication delay."""
    if now.tzinfo is None:
        raise ValueError('now must include its timezone')
    cutoff = now.astimezone(UTC) - timedelta(minutes=15)
    # End is exclusive: a bar beginning 15min before cutoff has just completed.
    return cutoff.replace(minute=(cutoff.minute // 15) * 15, second=0, microsecond=0)
