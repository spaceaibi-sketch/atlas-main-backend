"""Fetch one explicitly requested UTC period of 15-minute bars per company.

Historical requests always cover the requested period; the optional Massive
repair pass checks interior exchange-session gaps after the initial merge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from shared.processing_log import log_step
from .storage import ingest_bars

UTC = timezone.utc


@dataclass(frozen=True)
class PricePeriod:
    start: datetime
    end: datetime


@dataclass
class Result:
    company_id: int
    symbol: str
    received: int = 0
    saved: int = 0
    requests: int = 0
    gaps_unresolved: int = 0
    diagnostic_batches: list[dict] = field(default_factory=list)


def ranked_companies(conn, symbol=None, limit=None):
    """Newest filing/news update first, then previous cumulative-impact order.

    Uses stored, UTC activity timestamps; no guesswork about stock prices. News
    is associated only through news_company_relations.company_id. Missing tables
    in a partially initialized DB simply contribute no activity.
    """
    # One indexed pass over canonical event/company relationships.
    # Filing and news recency share the same saved source timestamp.
    cte = """WITH latest AS (
      SELECT r.company_id,
             MAX(COALESCE(NULLIF(s.event_at,''),NULLIF(s.updated_at,''))) last_activity
      FROM relationships r JOIN raw_items s ON s.id=r.source_id
      WHERE r.company_id IS NOT NULL AND s.source_type IN ('filing','news')
      GROUP BY r.company_id
    ) """
    sql = cte + """SELECT c.id,c.ticker,c.acumm_impact,
      COALESCE(latest.last_activity,'') last_activity
      FROM companies c LEFT JOIN latest ON latest.company_id=c.id
      WHERE c.ticker IS NOT NULL AND TRIM(c.ticker)<>'' """
    params = []
    if symbol:
        sql += 'AND UPPER(c.ticker)=UPPER(?) '
        params.append(symbol)
    sql += '''ORDER BY CASE WHEN last_activity='' THEN 1 ELSE 0 END,
        last_activity DESC,
        CASE WHEN ABS(COALESCE(c.acumm_impact,0))>0 THEN 0 ELSE 1 END,
        ABS(COALESCE(c.acumm_impact,0)) DESC, c.id ASC'''
    if limit is not None:
        sql += ' LIMIT ?'
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def requested_period(now: datetime, days: int) -> PricePeriod:
    """Return the exact requested lookback, independent of stored bars.

    Yahoo-specific limits are validated by the CLI, never by silently changing
    the requested period. Non-trading sessions simply return no bars.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must include timezone')
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError('--last must be a positive integer of days')
    end = now.astimezone(UTC)
    return PricePeriod(end - timedelta(days=days), end)


def collect_company(conn, client, row, period: PricePeriod, *, now: datetime,
                    dry_run=False, on_request=None, prefetched=None,
                    repair_gaps=False, verify_gaps=False):
    """Request exactly one provider period; insert valid 15m candles once.

    Existing rows are ignored via the database primary key. No SELECT against
    price_bars or gap scan is involved in scheduling the request.
    """
    company_id, symbol = row['id'], row['ticker'].strip()
    result = Result(company_id=company_id, symbol=symbol)
    log_step('price.collect', 'planned', company_id=company_id, symbol=symbol,
             source=getattr(client, 'source', 'alpaca'),
             start=period.start.isoformat(), end=period.end.isoformat(),
             dry_run=dry_run)
    if on_request:
        on_request(result, period)
    if dry_run:
        return result
    log_step('price.collect', 'request', company_id=company_id, symbol=symbol,
             start=period.start, end=period.end, feed=client.feed)
    result.requests = 1
    try:
        bars = prefetched if prefetched is not None else client.bars(symbol, period.start, period.end)
    except Exception as exc:
        log_step('price.collect', 'request_failed', company_id=company_id,
                 symbol=symbol, reason=str(exc)[:220], level='error')
        raise
    result.received = len(bars)
    log_step('price.collect', 'response', company_id=company_id, symbol=symbol,
             received=result.received)
    source = getattr(client, 'source', 'alpaca')
    diagnostic = {'batch': 'initial'} if source == 'massive' else None
    result.saved = ingest_bars(conn, company_id, bars, period.start, period.end,
                               client.feed, source=source, now=now,
                               diagnostics=diagnostic)
    if diagnostic is not None:
        result.diagnostic_batches.append(diagnostic)
    if (repair_gaps or verify_gaps) and getattr(client, 'source', None) == 'massive':
        from .calendar import safe_end, STEP
        from .storage import missing_regular_slots
        # Do not examine incomplete or unpublished bars, or fabricate trades.
        checked_end = min(period.end, safe_end(now))
        if checked_end > period.start:
            missing = missing_regular_slots(conn=conn, company_id=company_id,
                                            start=period.start, end=checked_end)
            if missing and repair_gaps:
                log_step('price.collect', 'gap_repair_requested',
                         company_id=company_id, symbol=symbol, gaps=len(missing))
                # One bounded request covering every candidate interior hole.
                result.requests += 1
                retry = client.bars(symbol, missing[0], missing[-1] + STEP)
                result.received += len(retry)
                repair_diagnostic = {'batch': 'gap_repair'}
                result.saved += ingest_bars(conn, company_id, retry,
                                            missing[0], missing[-1] + STEP,
                                            client.feed, source='massive', now=now,
                                            diagnostics=repair_diagnostic)
                result.diagnostic_batches.append(repair_diagnostic)
            result.gaps_unresolved = len(missing_regular_slots(
                conn, company_id, period.start, checked_end))
            if result.gaps_unresolved:
                log_step('price.collect', 'gaps_unresolved', company_id=company_id,
                         symbol=symbol, gaps=result.gaps_unresolved)
    log_step('price.collect', 'finished', company_id=company_id, symbol=symbol,
             received=result.received, saved=result.saved)
    return result
