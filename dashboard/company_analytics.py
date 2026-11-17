"""Seven-day company analytics, calculated only from completed stored AI assessments.

Exact persisted companies columns follow the user-requested names.  The materialized
figures are caches: apply a deterministic linear age weight in [0, 1] on the
published/filing timestamp, and recompute at read time and after AI batches.
No market data, guessed prices, or non-analysed placeholder zeroes participate.
"""
from __future__ import annotations
from datetime import datetime, timezone
from shared.processing_log import log_step

WINDOW_SECONDS = 7 * 24 * 60 * 60

# Reuse the same typed components for aggregation and explanation.  SEC Items are
# dated by the source filing timestamp, never their local parse or AI run date.
# Each company/news relationship is one component; duplicate news are excluded.
def _epoch(now):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now requires a timezone')
    return int(now.timestamp())


def _evidence_cte(conn):
    """Use only persisted canonical source/assessment records."""
    sql = """
       SELECT r.company_id, s.source_type, CASE WHEN s.source_type='item'
           THEN CAST(COALESCE(json_extract(s.metadata_json,'$.id'),s.id) AS TEXT)
           ELSE s.source_key END AS source_id,
         s.event_at event_time, CAST(strftime('%s',s.event_at) AS INTEGER) event_second,
         CASE WHEN s.source_type='item' THEN
           COALESCE(s.source_name,'SEC') || ' · Item ' ||
           COALESCE(json_extract(s.metadata_json,'$.item_type'),'') || ' · ' || s.title
           ELSE s.title END title,
         s.source_url, CASE WHEN s.source_type='news' THEN s.source_name ELSE '' END publisher,
         r.correlation_reason,
         r.ai_impact impact,r.ai_confidence confidence,
         r.ai_monetary_effect monetary,r.ai_reason reason,
         r.ai_evidence evidence,r.ai_monetary_effect_reason monetary_reason,
         r.price_change_1w_pct price_pct,r.price_change_reason price_reason
       FROM relationships r JOIN raw_items s ON s.id=r.source_id
       WHERE r.company_id IS NOT NULL AND r.entity_type='company'
         AND s.source_type IN ('item','news') AND r.ai_impact IS NOT NULL
         AND s.status IN ('connected','analysed')
    """
    return f"""WITH evidence AS ({sql}),
        recent AS MATERIALIZED (
          SELECT *, (1.0 - (:high - event_second) / {WINDOW_SECONDS}.0) age_weight
          FROM evidence WHERE event_second BETWEEN :low AND :high
        )"""


def refresh_company_aggregates(conn, now=None):
    """Refresh all persistent company totals in one SQL statement (no per-company loops).

    acumm_impact = SUM(impact * age_weight * confidence / 10)
    acumm_monetary = SUM(monetary * age_weight), USD
    accum_confdent = SUM(confidence * age_weight) / SUM(age_weight), 0..10

    These are age-weighted analytical indicators, NOT measured stock returns.
    Historical `0` monetary fields may mean unquantifiable, not observed zero.
    """
    high = _epoch(now)
    sql = _evidence_cte(conn) + """,
    totals AS MATERIALIZED (
       SELECT company_id,
          ROUND(SUM(impact * age_weight * confidence / 10.0), 4) impact_total,
          ROUND(SUM(monetary * age_weight), 2) monetary_total,
          ROUND(SUM(confidence * age_weight) / NULLIF(SUM(age_weight), 0), 4) confidence_avg
       FROM recent GROUP BY company_id
    ), new_values AS MATERIALIZED (
       SELECT c.id,
         COALESCE(t.impact_total, 0) impact_total,
         COALESCE(t.monetary_total, 0) monetary_total,
         COALESCE(t.confidence_avg, 0) confidence_avg
       FROM companies c LEFT JOIN totals t ON t.company_id=c.id
    )
    UPDATE companies SET
       acumm_impact=(SELECT impact_total FROM new_values v WHERE v.id=companies.id),
       acumm_monetary=(SELECT monetary_total FROM new_values v WHERE v.id=companies.id),
       accum_confdent=(SELECT confidence_avg FROM new_values v WHERE v.id=companies.id)
    WHERE acumm_impact IS NOT (SELECT impact_total FROM new_values v WHERE v.id=companies.id)
       OR acumm_monetary IS NOT (SELECT monetary_total FROM new_values v WHERE v.id=companies.id)
       OR accum_confdent IS NOT (SELECT confidence_avg FROM new_values v WHERE v.id=companies.id)
    RETURNING id,acumm_impact,acumm_monetary,accum_confdent
    """
    with conn:
        cursor = conn.execute(sql, {'low': high-WINDOW_SECONDS, 'high': high})
        changed = [dict(r) for r in cursor.fetchall()]
    for row in changed:
        log_step('company.analytics','updated',company_id=row['id'],seven_day_impact=row['acumm_impact'],
                 seven_day_monetary_usd=row['acumm_monetary'],seven_day_confidence=row['accum_confdent'])
    log_step('company.analytics','refresh_completed',updated=len(changed),window_seconds=WINDOW_SECONDS)
    return len(changed)


def company_assessments(db, company_id, now=None):
    """All eligible last-week assessed Items and unique main-event news for one company."""
    high = _epoch(now)
    sql = _evidence_cte(db) + """
       SELECT source_type, source_id, event_time, title, source_url, publisher,
              correlation_reason, impact, confidence, monetary, reason, evidence,
              monetary_reason, price_pct, price_reason, age_weight,
              ROUND((:high-event_second)/86400.0, 2) age_days
       FROM recent WHERE company_id=:company_id
       ORDER BY event_second DESC, source_type, source_id
    """
    return [dict(r) for r in db.execute(sql, {
        'low': high-WINDOW_SECONDS, 'high': high, 'company_id': company_id
    })]

if __name__ == '__main__':
    from contextlib import closing
    from sec_edgar.database import connect
    from sec_edgar.settings import database_path
    from news_pipeline.store import ensure_schema

    with closing(connect(database_path())) as _conn:
        ensure_schema(_conn)
        refresh_company_aggregates(_conn)
    print('Seven-day company analytics refreshed.')
