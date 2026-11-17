#!/usr/bin/env python3
"""Read-only audit of Atlas 15-minute price bars, mobile cache and chart visibility.

Does NOT modify the database. Uses sqlite3 mode=ro (never creates a new DB).
Examples:
    python3 scripts/check_price_chart.py --db /opt/atlas/data/edgar.sqlite3
    python3 scripts/check_price_chart.py --db /opt/atlas/data/edgar.sqlite3 --symbol AAPL
    python3 scripts/check_price_chart.py --db /opt/atlas/data/edgar.sqlite3 --company-id 42 --days 30
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

TABLES = ('companies', 'forms', 'items', 'news', 'news_company_relations',
          'price_bars', 'summary')
REQUIRED_PRICE_COLUMNS = ('company_id', 'bar_time_utc', 'open', 'high', 'low',
                          'close', 'volume', 'source', 'feed')


def print_rows(title, rows):
    print(f'\n{title}')
    for row in rows:
        print('  ' + ' | '.join(f'{key}={value!s}' for key, value in dict(row).items()))
    if not rows:
        print('  (none)')


def audit(db_path: Path, *, days: int = 30, symbol: str | None = None,
          company_id: int | None = None, api_config: Path | None = None,
          now: datetime | None = None) -> int:
    path = db_path.expanduser().resolve()
    if not path.is_file():
        print(f'ERROR: SQLite file is missing: {path}', file=sys.stderr)
        return 2
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'DB: {path}')
    print(f'UTC now: {now.strftime("%Y-%m-%dT%H:%M:%SZ")}')
    print(f'Chart lookback: {days} calendar days; cutoff={cutoff}')
    print('Mode: READ ONLY — no SQLite schema changes, updates or inserts')

    if api_config and api_config.is_file():
        properties = {}
        for line in api_config.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith(('#', '!')) and '=' in line:
                key, value = line.split('=', 1)
                properties[key.strip()] = value.strip()
        api_db = properties.get('sec.db.path')
        if api_db:
            other = Path(api_db).expanduser().resolve()
            print(f'Java service sec.db.path: {other}')
            print(f'SAME SQLITE FILE: {other == path}')
            if other != path:
                print('WARNING: Java API and extractor may read different databases!')
    elif api_config:
        print(f'NOTE: Java config was not readable: {api_config}')

    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        print('\nTABLE ROW COUNTS (price extraction should update price_bars only)')
        for name in TABLES:
            print(f'  {name}: {db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] if name in tables else "MISSING"}')
        if not {'companies', 'price_bars'} <= tables:
            print('ERROR: companies or price_bars table is missing; chart has no usable data.')
            return 1
        cols = {row[1] for row in db.execute('PRAGMA table_info(price_bars)')}
        missing = set(REQUIRED_PRICE_COLUMNS) - cols
        if missing:
            print(f'ERROR: price_bars is missing columns: {sorted(missing)}')
            return 1

        total = db.execute('''SELECT COUNT(*) AS total, COUNT(DISTINCT company_id) AS companies,
          SUM(CASE WHEN close IS NULL OR close<=0 THEN 1 ELSE 0 END) AS invalid_close,
          MIN(bar_time_utc) AS first_bar, MAX(bar_time_utc) AS latest_bar,
          SUM(CASE WHEN bar_time_utc>=? THEN 1 ELSE 0 END) AS bars_in_chart_window,
          COUNT(DISTINCT CASE WHEN bar_time_utc>=? THEN company_id END) AS companies_in_chart_window
          FROM price_bars''', (cutoff, cutoff)).fetchone()
        print('\nPRICE TABLE CHECK')
        for key, value in dict(total).items():
            print(f'  {key}: {value}')
        interval_rows = db.execute('''SELECT bar_time_utc FROM price_bars
            ORDER BY bar_time_utc DESC LIMIT 2000''').fetchall()
        wrong_intervals = [r[0] for r in interval_rows if len(r[0]) < 20
                           or r[0][14:16] not in ('00','15','30','45')
                           or r[0][17:19] != '00']
        print(f'  sampled_non_15min_timestamps: {len(wrong_intervals)}/{len(interval_rows)}')
        if wrong_intervals:
            print('  WARNING: some sampled timestamps do not align with 15-minute bars: ' + repr(wrong_intervals[:3]))
        print_rows('PRICE PROVIDERS', db.execute('''SELECT source,feed,COUNT(*) AS bars,
          MAX(bar_time_utc) AS latest_bar,MAX(ingested_at) AS latest_ingested
          FROM price_bars GROUP BY source,feed ORDER BY bars DESC''').fetchall())
        print_rows('MOST RECENT 10 CLOSES (across companies)', db.execute('''SELECT c.id company_id,
          c.ticker,p.bar_time_utc,p.close,p.source,p.feed
          FROM price_bars p LEFT JOIN companies c ON c.id=p.company_id
          ORDER BY p.bar_time_utc DESC LIMIT 10''').fetchall())

        summary_ready = 'summary' in tables
        if summary_ready:
            c = db.execute('''WITH latest AS (
                SELECT company_id,MAX(bar_time_utc) AS latest_bar
                FROM price_bars GROUP BY company_id)
                SELECT COUNT(*) AS companies_with_prices,
                SUM(CASE WHEN s.company_id IS NULL THEN 1 ELSE 0 END) AS no_summary_row,
                SUM(CASE WHEN s.last_price IS NULL THEN 1 ELSE 0 END) AS null_cached_close,
                SUM(CASE WHEN s.last_price_bar_at IS NULL OR s.last_price_bar_at<>p.bar_time_utc
                         OR s.last_price IS NOT p.close THEN 1 ELSE 0 END) AS outdated_cached_price
                FROM latest l JOIN price_bars p ON p.company_id=l.company_id AND p.bar_time_utc=l.latest_bar
                LEFT JOIN summary s ON s.company_id=l.company_id''').fetchone()
            print('\nMOBILE COMPANY SUMMARY CACHE')
            for key, val in dict(c).items():
                print(f'  {key}: {val}')
            if c['outdated_cached_price']:
                print('WARNING: mobile summary latest close differs from price_bars.')
                print('  Repair command: python3 -m dashboard.mobile_summary --db ' + str(path))
        else:
            print('\nMOBILE COMPANY SUMMARY CACHE: missing (not populated by price extraction)')

        where, params = [], []
        if company_id is not None:
            where.append('c.id=?'); params.append(company_id)
        if symbol is not None:
            where.append('UPPER(TRIM(c.ticker))=UPPER(TRIM(?))'); params.append(symbol)
        if where:
            company_where = ' WHERE ' + ' AND '.join(where)
            candidates = db.execute('SELECT id,ticker,name FROM companies c' + company_where, params).fetchall()
            if not candidates:
                print(f'\nERROR: no matching company: symbol={symbol!r}, company_id={company_id!r}')
                return 1
        else:
            candidates = db.execute('''SELECT c.id,c.ticker,c.name FROM companies c
                JOIN price_bars p ON p.company_id=c.id
                GROUP BY c.id ORDER BY MAX(p.bar_time_utc) DESC LIMIT 10''').fetchall()

        print('\nCOMPANY CHART DATA (same 30-day default as Java /prices endpoint)')
        for candidate in candidates:
            cid = candidate['id']
            observed = db.execute('''SELECT COUNT(*) AS total_stored,
                 MAX(bar_time_utc) AS latest_bar FROM price_bars WHERE company_id=?''', (cid,)).fetchone()
            recent = db.execute('''SELECT COUNT(*) AS shown_in_chart,
                 MAX(bar_time_utc) AS latest_visible
                 FROM price_bars WHERE company_id=? AND bar_time_utc>=?''', (cid, cutoff)).fetchone()
            latest = db.execute('''SELECT close,bar_time_utc,source,feed FROM price_bars
                 WHERE company_id=? ORDER BY bar_time_utc DESC LIMIT 1''', (cid,)).fetchone()
            print(f'  id={cid} ticker={candidate["ticker"]!r} total={observed["total_stored"]} '
                  f'chart_window={recent["shown_in_chart"]} '
                  f'latest_bar={observed["latest_bar"]} '
                  f'latest_close={latest["close"] if latest else None} '
                  f'source={latest["source"] if latest else None}')
            if summary_ready:
                summary = db.execute('''SELECT last_price,last_price_bar_at,refreshed_at
                    FROM summary WHERE company_id=?''', (cid,)).fetchone()
                print(f'    cached_close={summary["last_price"] if summary else None} '
                      f'cached_at={summary["last_price_bar_at"] if summary else None} '
                      f'cache_refreshed={summary["refreshed_at"] if summary else None}')
            if not recent['shown_in_chart']:
                print('    CHART WILL BE EMPTY FOR THIS LOOKBACK: no bars inside selected period')
            elif recent['shown_in_chart'] == 1:
                print('    DASH CHART MAY LOOK EMPTY: a lines-only Plotly chart needs >=2 points to draw a line')
        print('\nDIAGNOSIS RULES')
        print('  Bar close in price_bars + chart_window>0: Java API should have stored chart data.')
        print('  Bar close present but cached_close missing/different: refresh summary.')
        print('  Bar close present but chart_window=0: increase the API /prices?days=N range.')
        print('  Chart_window>0 but Android chart blank: verify HTTP /prices or /timeline JSON and client renderer.')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=Path(os.getenv('SEC_DB_PATH', '/opt/atlas/data/edgar.sqlite3')))
    parser.add_argument('--api-config', type=Path, default=Path('/opt/atlas/config/atlas.properties'))
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--symbol', type=str)
    parser.add_argument('--company-id', type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 365:
        parser.error('--days must be 1..365')
    try:
        return audit(args.db, days=args.days, symbol=args.symbol, company_id=args.company_id,
                     api_config=args.api_config)
    except sqlite3.Error as exc:
        print(f'ERROR: SQLite could not be read: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
