"""Strict UTC publication window for news ingestion and UTC date comparisons."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from shared.config import number as config_num

NEWS_MAX_AGE = timedelta(hours=3) # historic default, unchanged when .env is absent
# RSS clocks can run a few minutes ahead. Reject larger future timestamps.
MAX_FUTURE_SKEW = timedelta(0)


def utc_now():
    return datetime.now(timezone.utc)


def ensure_utc_now(value=None):
    """UTC-normalize test/injected clocks; never interpret local naive times."""
    if value is None:
        return utc_now()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('News processing clock must be timezone-aware')
    return value.astimezone(timezone.utc)


def read_utc(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def publication_status(value, now=None):
    """Only articles published within the last 3 hours are eligible.

    Comparisons include the exactly-three-hours-old boundary. Missing time,
    older timestamps, and future timestamps are excluded.
    """
    now = ensure_utc_now(now)
    published = read_utc(value)
    if published is None:
        return 'missing_time'
    if published > now + MAX_FUTURE_SKEW:
        return 'future'
    if published < now - timedelta(hours=config_num('NEWS_INGEST_WINDOW_HOURS', 3, minimum=0.1)):
        return 'outside_window'
    return 'fresh'


def cutoff_iso(now=None):
    return (ensure_utc_now(now) - timedelta(hours=config_num('NEWS_INGEST_WINDOW_HOURS', 3, minimum=0.1))).isoformat(timespec='seconds')
