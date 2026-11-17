"""Materialized first-page mobile data; only the main backend writes this table.

SQLite set-based aggregates and indexed close lookups run at ingestion/AI refresh,
never from an Android API request. Price changes are observed close-to-close,
anchored on the latest available 15-minute candle.
"""
from __future__ import annotations

from datetime import datetime, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS summary (
 company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
 latest_published TEXT,
 last_news_received_at TEXT,
 latest_filing TEXT,
 latest_item_filed TEXT,
 related_news_count INTEGER NOT NULL DEFAULT 0,
 scored_news_count INTEGER NOT NULL DEFAULT 0,
 scored_items_count INTEGER NOT NULL DEFAULT 0,
 sec_items_count INTEGER NOT NULL DEFAULT 0,
 num_forms INTEGER NOT NULL DEFAULT 0,
 aggregate_score REAL,
 max_news_impact REAL,
 max_sec_impact REAL,
 latest_event_impact REAL,
 latest_event_kind TEXT,
 latest_event_at TEXT,
 max_news_at TEXT,
 max_sec_at TEXT,
 last_assessed_at TEXT,
 latest_confidence REAL,
 last_price REAL,
 last_price_bar_at TEXT,
 trend_24h REAL,
 trend_7d REAL,
 refreshed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mobile_news ON summary(latest_published DESC,company_id);
CREATE INDEX IF NOT EXISTS idx_mobile_filing ON summary(latest_filing DESC,company_id);
CREATE INDEX IF NOT EXISTS idx_mobile_items ON summary(latest_item_filed DESC,company_id);
CREATE INDEX IF NOT EXISTS idx_mobile_score ON summary(aggregate_score DESC,company_id);
'''


IMPACT_FIELDS = ('max_news_impact', 'max_sec_impact', 'latest_event_impact',
                 'latest_event_kind', 'latest_event_at', 'max_news_at', 'max_sec_at',
                 'last_assessed_at', 'latest_confidence')


def ensure_summary_schema(db):
    # sqlite3.executescript() implicitly commits an open transaction, even when
    # all CREATE IF NOT EXISTS statements are no-ops. That would let a mobile
    # cache refresh commit unrelated writer changes before the caller does.
    # Execute the idempotent schema statements individually in the caller's
    # current transaction instead.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    columns = {row[1] for row in db.execute('PRAGMA table_info(summary)')}
    if not set(IMPACT_FIELDS).issubset(columns):
        raise RuntimeError(
            'summary is missing the new Impact columns. '
            'Stop ingestion and run: python3 scripts/migrate_mobile_impact_summary.py '
            '--db /path/to/atlas.sqlite3 --apply'
        )


def _has(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def refresh_company_summaries(db, *, company_id=None, now=None, refresh_prices=True):
    """Atomically materialize stored canonical News/SEC records into summary.

    Only the five real Atlas tables are queried (no legacy compatibility views).
    Incoming/news/SEC relations are reflected even before AI scoring. The
    impact total includes all real, non-skipped company assessments whose
    relationship has an actual AI impact, consistent with the API contract.

    For scheduled News/SEC ingestion refreshes, refresh_prices=False avoids
    rescanning bars and keeps the independently trigger-maintained prices.
    """
    ensure_summary_schema(db)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    scope = 'c.id=:company_id' if company_id is not None else '1=1'
    price_scope = ' WHERE company_id=:company_id' if company_id is not None else ''
    has_price = refresh_prices and _has(db, 'price_bars')
    price_cte = (f'''
    latest AS (
       SELECT company_id,MAX(bar_time_utc) bar_time_utc
       FROM price_bars{price_scope} GROUP BY company_id
    ),
    last_prices AS (
       SELECT p.company_id,p.bar_time_utc,p.close FROM price_bars p
       JOIN latest l ON l.company_id=p.company_id AND l.bar_time_utc=p.bar_time_utc
    ),
    price_stats AS (
       SELECT lp.company_id,lp.bar_time_utc,lp.close,
         (SELECT p.close FROM price_bars p WHERE p.company_id=lp.company_id
            AND p.close IS NOT NULL AND julianday(p.bar_time_utc)<=julianday(lp.bar_time_utc)-1
            ORDER BY p.bar_time_utc DESC LIMIT 1) prev_24,
         (SELECT p.close FROM price_bars p WHERE p.company_id=lp.company_id
            AND p.close IS NOT NULL AND julianday(p.bar_time_utc)<=julianday(lp.bar_time_utc)-7
            ORDER BY p.bar_time_utc DESC LIMIT 1) prev_7
       FROM last_prices lp
    )''' if has_price else 'price_stats AS (SELECT NULL company_id,NULL bar_time_utc,NULL close,NULL prev_24,NULL prev_7 WHERE 0)')
    # Normalize ISO 8601 offsets to UTC before MAX comparisons, never compare
    # raw clock strings (20:00+03 is earlier than 17:30Z).
    sql = f'''WITH
    news_stats AS (
      SELECT r.company_id,
        COUNT(DISTINCT CASE WHEN s.status IN ('connected','analysed') THEN s.id END) n,
        SUM(CASE WHEN r.ai_impact IS NOT NULL AND s.status!='skipped' THEN 1 ELSE 0 END) scored,
        SUM(CASE WHEN s.status!='skipped' THEN r.ai_impact END) impact,
        MAX(CASE WHEN s.status IN ('connected','analysed')
            THEN strftime('%Y-%m-%dT%H:%M:%fZ',s.event_at) END) latest,
        MAX(CASE WHEN s.status IN ('connected','analysed')
            THEN strftime('%Y-%m-%dT%H:%M:%fZ',s.received_at) END) received
      FROM relationships r JOIN raw_items s ON s.id=r.source_id
      WHERE r.entity_type='company' AND r.company_id IS NOT NULL AND s.source_type='news'
      GROUP BY r.company_id
    ),
    form_stats AS (
      SELECT r.company_id,COUNT(DISTINCT s.id) n,MAX(strftime('%Y-%m-%dT%H:%M:%fZ',s.event_at)) latest
      FROM relationships r JOIN raw_items s ON s.id=r.source_id
      WHERE r.entity_type='company' AND r.company_id IS NOT NULL
        AND s.source_type='filing' AND s.status!='skipped'
      GROUP BY r.company_id
    ),
    item_stats AS (
      SELECT r.company_id,COUNT(DISTINCT s.id) n,
        SUM(CASE WHEN r.ai_impact IS NOT NULL THEN 1 ELSE 0 END) scored,
        SUM(r.ai_impact) impact,MAX(strftime('%Y-%m-%dT%H:%M:%fZ',s.event_at)) latest
      FROM relationships r JOIN raw_items s ON s.id=r.source_id
      WHERE r.entity_type='company' AND r.company_id IS NOT NULL
        AND s.source_type='item' AND s.status!='skipped'
      GROUP BY r.company_id
    ),
    {price_cte}
    INSERT INTO summary(
      company_id,latest_published,last_news_received_at,latest_filing,latest_item_filed,
      related_news_count,scored_news_count,scored_items_count,sec_items_count,num_forms,
      aggregate_score,last_price,last_price_bar_at,trend_24h,trend_7d,refreshed_at)
    SELECT c.id,n.latest,n.received,f.latest,i.latest,
      COALESCE(n.n,0),COALESCE(n.scored,0),COALESCE(i.scored,0),COALESCE(i.n,0),COALESCE(f.n,0),
      CASE WHEN n.impact IS NULL AND i.impact IS NULL THEN NULL
           ELSE COALESCE(n.impact,0)+COALESCE(i.impact,0) END,
      p.close,p.bar_time_utc,
      CASE WHEN p.prev_24 IS NOT NULL AND p.prev_24!=0
           THEN ROUND(100.0*(p.close/p.prev_24-1),6) END,
      CASE WHEN p.prev_7 IS NOT NULL AND p.prev_7!=0
           THEN ROUND(100.0*(p.close/p.prev_7-1),6) END,
      :now
    FROM companies c
    LEFT JOIN news_stats n ON n.company_id=c.id
    LEFT JOIN form_stats f ON f.company_id=c.id
    LEFT JOIN item_stats i ON i.company_id=c.id
    LEFT JOIN price_stats p ON p.company_id=c.id
    WHERE {scope} AND 1=1
    ON CONFLICT(company_id) DO UPDATE SET
      latest_published=excluded.latest_published,
      last_news_received_at=excluded.last_news_received_at,
      latest_filing=excluded.latest_filing,
      latest_item_filed=excluded.latest_item_filed,
      related_news_count=excluded.related_news_count,
      scored_news_count=excluded.scored_news_count,
      scored_items_count=excluded.scored_items_count,
      sec_items_count=excluded.sec_items_count,
      num_forms=excluded.num_forms,
      aggregate_score=excluded.aggregate_score,
      last_price={'excluded.last_price' if refresh_prices else 'summary.last_price'},
      last_price_bar_at={'excluded.last_price_bar_at' if refresh_prices else 'summary.last_price_bar_at'},
      trend_24h={'excluded.trend_24h' if refresh_prices else 'summary.trend_24h'},
      trend_7d={'excluded.trend_7d' if refresh_prices else 'summary.trend_7d'},
      refreshed_at=excluded.refreshed_at'''
    impact_sql = """WITH scored AS (
      SELECT r.company_id,s.source_type AS kind,r.id record_id,r.ai_impact impact,
        r.ai_confidence confidence,r.updated_at assessed_at,s.event_at event_at
      FROM relationships r JOIN raw_items s ON s.id=r.source_id
      WHERE r.ai_impact IS NOT NULL AND r.company_id IS NOT NULL
        AND r.entity_type='company'
        AND s.source_type IN ('news','item') AND s.status!='skipped'
        AND (:company_id IS NULL OR r.company_id=:company_id)
    ), ranked AS (
      SELECT *,
       ROW_NUMBER() OVER(PARTITION BY company_id,kind
          ORDER BY ABS(impact) DESC,julianday(event_at) DESC,record_id DESC) AS kind_rank,
       ROW_NUMBER() OVER(PARTITION BY company_id
          ORDER BY julianday(event_at) DESC,kind,record_id DESC) AS event_rank,
       ROW_NUMBER() OVER(PARTITION BY company_id
          ORDER BY julianday(assessed_at) DESC,kind,record_id DESC) AS assessed_rank
      FROM scored
    ), totals AS (
      SELECT company_id,
        MAX(CASE WHEN kind='news' AND kind_rank=1 THEN impact END) max_news_impact,
        MAX(CASE WHEN kind='item' AND kind_rank=1 THEN impact END) max_sec_impact,
        MAX(CASE WHEN event_rank=1 THEN impact END) latest_event_impact,
        MAX(CASE WHEN event_rank=1 THEN kind END) latest_event_kind,
        MAX(CASE WHEN event_rank=1 THEN strftime('%Y-%m-%dT%H:%M:%fZ',event_at) END) latest_event_at,
        MAX(CASE WHEN kind='news' AND kind_rank=1 THEN strftime('%Y-%m-%dT%H:%M:%fZ',event_at) END) max_news_at,
        MAX(CASE WHEN kind='item' AND kind_rank=1 THEN strftime('%Y-%m-%dT%H:%M:%fZ',event_at) END) max_sec_at,
        MAX(strftime('%Y-%m-%dT%H:%M:%fZ',assessed_at)) last_assessed_at,
        MAX(CASE WHEN assessed_rank=1 THEN confidence END) latest_confidence
      FROM ranked GROUP BY company_id
    )
    UPDATE summary SET
      max_news_impact=(SELECT max_news_impact FROM totals WHERE company_id=summary.company_id),
      max_sec_impact=(SELECT max_sec_impact FROM totals WHERE company_id=summary.company_id),
      latest_event_impact=(SELECT latest_event_impact FROM totals WHERE company_id=summary.company_id),
      latest_event_kind=(SELECT latest_event_kind FROM totals WHERE company_id=summary.company_id),
      latest_event_at=(SELECT latest_event_at FROM totals WHERE company_id=summary.company_id),
      max_news_at=(SELECT max_news_at FROM totals WHERE company_id=summary.company_id),
      max_sec_at=(SELECT max_sec_at FROM totals WHERE company_id=summary.company_id),
      last_assessed_at=(SELECT last_assessed_at FROM totals WHERE company_id=summary.company_id),
      latest_confidence=(SELECT latest_confidence FROM totals WHERE company_id=summary.company_id)
    WHERE (:company_id IS NULL OR company_id=:company_id)
    """
    with db:
        # Reserve the single SQLite writer before the expensive read+aggregate.
        # With concurrent pipelines this prevents DEFERRED read-to-write lock
        # upgrades (SQLITE_BUSY / SQLITE_BUSY_SNAPSHOT under WAL).
        # Busy timeout in sec_edgar.database.connect() applies while waiting.
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        db.execute(sql, {'now':now,'company_id':company_id})
        db.execute(impact_sql, {'company_id':company_id})
    return db.execute('SELECT COUNT(*) FROM summary').fetchone()[0]


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    from sec_edgar.database import connect
    from sec_edgar.settings import database_path
    parser=argparse.ArgumentParser(description='Rebuild Android first-page summaries from stored source data')
    parser.add_argument('--db',type=Path,default=None)
    parser.add_argument('--events-only',action='store_true',
        help='Recompute news/SEC and impact; preserve price fields maintained by price triggers')
    args=parser.parse_args(argv)
    with closing(connect(args.db or database_path())) as db:
        count=refresh_company_summaries(db,refresh_prices=not args.events_only)
    print(f'Refreshed summary: {count} company rows')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
