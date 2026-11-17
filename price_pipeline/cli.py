"""Run with: python -m price_pipeline.cli --source alpaca|yfinance|massive --last DAYS."""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import local
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from shared.processing_log import log_step
from shared.config import text as config_text, integer as config_int, number as config_num
from sec_edgar.database import connect
from sec_edgar.settings import database_path
from .alpaca import AlpacaClient, AlpacaError
from .yfinance import YFinanceClient, YFinanceError
from .massive import MassiveClient, MassiveError, SharedRateLimiter
from .collector import collect_company, ranked_companies, requested_period
from .storage import ensure_yfinance_feed, export_company_csv


def parser():
    p = argparse.ArgumentParser(description='Fetch and merge 15-minute bars; Massive checks and repairs interior session gaps')
    p.add_argument('--db', type=Path, default=None, help='Shared SEC SQLite path')
    p.add_argument('--source', choices=['alpaca', 'yfinance', 'massive'],
                   default=config_text('PRICE_SOURCE', 'alpaca').lower(),
                   help='Price provider (default PRICE_SOURCE, otherwise alpaca)')
    p.add_argument('--last', type=int, required=True, metavar='DAYS',
                   help='Required: download the entire last DAYS calendar days (15-minute bars)')
    p.add_argument('-thread', '--threads', '--thread', type=int, default=None, metavar='N',
                   help='Massive fetch worker threads, e.g. -thread 4 (1-32; default MASSIVE_THREADS or 4)')
    p.add_argument('--no-gap-repair', action='store_true',
                   help='Skip additional Massive request for gaps inside observed sessions')
    p.add_argument('--strict-gaps', action='store_true',
                   help='Return failure if any interior session gap remains after Massive repair')
    p.add_argument('--symbol', help='Fetch one ticker (case-insensitive)')
    p.add_argument('--limit', type=int, help='Maximum companies, most recent news/filing first')
    p.add_argument('--feed', choices=['sip', 'iex'], default=None,
                   help='Only for Alpaca (default ALPACA_FEED, otherwise sip)')
    p.add_argument('--export-dir', type=Path, help='Export company CSV after processing')
    p.add_argument('--dry-run', action='store_true', help='Print the exact requested period, no downloads or inserts')
    p.add_argument('--refresh-impact', action='store_true',
                   help='Opt-in cumulative-impact refresh before collection; off by default')
    p.add_argument('--no-refresh-impact', action='store_true',
                   help=argparse.SUPPRESS)  # old flag: already the default
    return p


def load_alpaca_env(base: Path):
    """Compatibility helper: load environment settings without shell evaluation."""
    from sec_edgar.windows_launcher import load_env
    load_env(base / '.env')


def say(message, *, err=False):
    print(message, file=sys.stderr if err else sys.stdout, flush=True)


def run(argv=None, *, client_factory=AlpacaClient, now=None,
        yfinance_factory=YFinanceClient, massive_factory=MassiveClient):
    p = parser()
    args = p.parse_args(argv)
    threads = (args.threads if args.threads is not None else
               config_int('MASSIVE_THREADS', 4, minimum=1, maximum=32)) if args.source == 'massive' else 1
    if not 1 <= threads <= 32:
        p.error('-thread / --threads must be between 1 and 32')
    if args.source != 'massive' and (args.threads is not None or args.strict_gaps or args.no_gap_repair):
        p.error('threading and gap options require --source massive')
    if args.limit is not None and args.limit <= 0:
        p.error('--limit must be positive')
    if args.last <= 0:
        p.error('--last must be a positive integer of days')
    if args.source == 'yfinance' and args.last > 60:
        p.error('Yahoo 15-minute intraday data is limited to the last 60 days; use --last <= 60')
    if args.source != 'alpaca' and args.feed is not None:
        p.error('--feed is only available with --source alpaca')
    if args.refresh_impact and args.no_refresh_impact:
        p.error('--refresh-impact and --no-refresh-impact cannot be used together')
    now = now or datetime.now(timezone.utc)
    period = requested_period(now, args.last)
    db_path = args.db or database_path()
    say(f'[PRICE] START source={args.source} interval=15m last={args.last}d '
        f'from={period.start.isoformat()} to={period.end.isoformat()} '
        f'db={db_path} dry_run={args.dry_run} threads={threads}')
    log_step('price.run', 'started', symbol=args.symbol, limit=args.limit,
             source=args.source, days=args.last, dry_run=args.dry_run)
    key = None
    limiter = None
    if not args.dry_run:
        say('[PRICE] Loading provider configuration...')
        load_alpaca_env(Path(__file__).resolve().parents[1])
        if args.source == 'alpaca':
            key, secret = os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_API_SECRET')
            if not key or not secret:
                say('[PRICE] ERROR Missing ALPACA_API_KEY / ALPACA_API_SECRET (set in .env)', err=True)
                return 2
            client = client_factory(key, secret,
                                    feed=args.feed or config_text('ALPACA_FEED', 'sip'))
        elif args.source == 'massive':
            key = os.getenv('MASSIVE_API_KEY')
            if not key:
                say('[PRICE] ERROR Missing MASSIVE_API_KEY (set in private .env)', err=True)
                return 2
            limiter = SharedRateLimiter(config_num('MASSIVE_MIN_INTERVAL_SECONDS', 12.5, minimum=0))
            client = (massive_factory(key, limiter=limiter) if massive_factory is MassiveClient
                      else massive_factory(key))
        else:
            try:
                client = yfinance_factory()
            except YFinanceError as exc:
                say(f'[PRICE] ERROR {exc}', err=True)
                return 2
    else:
        # A dry run does not need provider installation or credentials.
        client = type('DryRunSource', (), {'source': args.source,
                       'feed': ('yfinance' if args.source == 'yfinance' else
                                'sip' if args.source == 'massive' else
                                args.feed or config_text('ALPACA_FEED', 'sip'))})()
    say('[PRICE] Opening SQLite database...')
    with closing(connect(db_path)) as db:
        if args.source == 'yfinance' and not args.dry_run:
            say('[PRICE] Checking SQLite feed schema (first use may require a backup/migration)...')
            migrated = ensure_yfinance_feed(db)
            if migrated:
                say('[PRICE] SQLite schema migrated; database backup created.')
        if args.refresh_impact and not args.dry_run:
            say('[PRICE] Refreshing company aggregates (explicitly requested)...')
            from dashboard.company_analytics import refresh_company_aggregates
            refresh_company_aggregates(db, now=now)
        say('[PRICE] Selecting companies by latest news/filing activity...')
        companies = ranked_companies(db, args.symbol, args.limit)
        if args.symbol and not companies:
            say(f'[PRICE] ERROR No company with ticker {args.symbol!r} in companies table', err=True)
            return 2
        say(f'[PRICE] Selected {len(companies)} companies. Downloading full requested period and repairing observed Massive session gaps.')
        if args.source == 'massive' and not args.dry_run:
            from .massive_writer import run_massive_queued
            from .durable_writer import SpoolError
            try:
                return run_massive_queued(
                    db_path=db_path, companies=companies, period=period, now=now,
                    client=client, key=key, limiter=limiter,
                    massive_factory=massive_factory, threads=threads,
                    no_gap_repair=args.no_gap_repair, strict_gaps=args.strict_gaps,
                    export_dir=args.export_dir, say=say)
            except (OSError, SpoolError) as exc:
                say(f'[PRICE QUEUE] FATAL: {exc}; unsaved work remains on disk', err=True)
                return 1
        errors = 0
        unresolved_gaps = 0
        # A bounded work queue prevents unbounded queued bar responses in RAM.
        # Worker sessions issue HTTP only. SQLite stays on this calling thread.
        executor = None
        pending = {}
        indexed_rows = iter(enumerate(companies, 1))
        if args.source == 'massive' and threads > 1 and not args.dry_run:
            thread_state = local()

            def fetch(row):
                worker = getattr(thread_state, 'client', None)
                if worker is None:
                    worker = (massive_factory(key, limiter=limiter)
                              if massive_factory is MassiveClient else massive_factory(key))
                    thread_state.client = worker
                return worker.bars(row['ticker'].strip(), period.start, period.end)

            executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix='massive-fetch')

            def submit_one():
                try:
                    index, row = next(indexed_rows)
                except StopIteration:
                    return False
                pending[executor.submit(fetch, row)] = (index, row)
                return True

            for _ in range(threads * 2):
                if not submit_one():
                    break
        total_saved = 0
        total_received = 0
        diagnostics_totals = Counter()
        diagnostics_reasons = Counter()
        diagnostics_sources = Counter()
        try:
            while True:
                if executor:
                    if not pending:
                        break
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    future = next(iter(done))
                    i, row = pending.pop(future)
                    submit_one()
                else:
                    try:
                        i, row = next(indexed_rows)
                    except StopIteration:
                        break
                    future = None
                symbol = row['ticker'].strip()
                try:
                    def progress(result, requested):
                        say(f'[PRICE] [{i}/{len(companies)}] '
                            f'{"PLAN" if args.dry_run else "FETCH"} '
                            f'{symbol} company={result.company_id} '
                            f'latest_activity={row["last_activity"] or "none"} '
                            f'from={requested.start.isoformat()} to={requested.end.isoformat()}')
                    bars = future.result() if future is not None else None
                    result = collect_company(db, client, row, period, now=now,
                                             dry_run=args.dry_run, on_request=progress,
                                             prefetched=bars,
                                             repair_gaps=(args.source == 'massive' and
                                                          not args.no_gap_repair and not args.dry_run),
                                             verify_gaps=args.strict_gaps)
                except (AlpacaError, YFinanceError, MassiveError) as exc:
                    errors += 1
                    say(f'[PRICE] [{i}/{len(companies)}] ERROR {symbol}: {exc}', err=True)
                    if ((isinstance(exc, AlpacaError) and 'credentials/feed' in str(exc)) or
                            (isinstance(exc, MassiveError) and 'credentials/plan' in str(exc))):
                        return 3
                    continue
                except Exception as exc:
                    errors += 1
                    say(f'[PRICE] [{i}/{len(companies)}] ERROR {symbol}: '
                        f'{type(exc).__name__}: {exc}', err=True)
                    continue
                total_saved += result.saved
                total_received += result.received
                unresolved_gaps += result.gaps_unresolved
                say(f'[PRICE] [{i}/{len(companies)}] '
                    f'{"PLANNED" if args.dry_run else "DONE"} {result.symbol} '
                    f'bars_received={result.received} bars_saved={result.saved} '
                    f'not_inserted={result.received - result.saved} '
                    f'gaps_unresolved={result.gaps_unresolved}')
                for diag in result.diagnostic_batches:
                    diagnostics_totals.update({
                        field: int(diag.get(field, 0)) for field in (
                            'received', 'valid_unique', 'rejected_invalid',
                            'duplicates_in_response', 'existing_keys', 'fresh_candidates',
                            'saved', 'sqlite_changes', 'missing_after_merge',
                            'volume_defaulted', 'trade_count_ignored', 'vwap_ignored')
                    })
                    diagnostics_reasons.update(diag['rejection_reasons'])
                    diagnostics_sources.update(diag['existing_by_source_feed'])
                    reasons = ','.join(f'{k}:{v}' for k, v in diag['rejection_reasons'].items() if v) or 'none'
                    origins = ','.join(f'{k}:{v}' for k, v in diag['existing_by_source_feed'].items()) or 'none'
                    say(f'[PRICE DIAG] {symbol} company={result.company_id} '
                        f'batch={diag["batch"]} received={diag["received"]} '
                        f'valid_unique={diag["valid_unique"]} '
                        f'invalid={diag["rejected_invalid"]} '
                        f'repeated_in_response={diag["duplicates_in_response"]} '
                        f'existing_keys={diag["existing_keys"]} '
                        f'fresh_candidates={diag["fresh_candidates"]} '
                        f'saved={diag["saved"]} sqlite_changes={diag["sqlite_changes"]} '
                        f'missing_after_merge={diag["missing_after_merge"]} '
                        f'db_rows_before={diag["company_rows_before"]} '
                        f'db_rows_after={diag["company_rows_after"]} '
                        f'first_valid={diag["first_valid_utc"]} last_valid={diag["last_valid_utc"]} '
                        f'db_first={diag["db_first_utc"]} db_last={diag["db_last_utc"]} '
                        f'existing_by_source={origins} rejected_by_reason={reasons} '
                        f'volume_defaulted={diag["volume_defaulted"]} '
                        f'trade_count_ignored={diag["trade_count_ignored"]} '
                        f'vwap_ignored={diag["vwap_ignored"]}')
                    if diag['merge_mismatch']:
                        say(f'[PRICE DIAG] WARNING {symbol}: insert-count mismatch or '
                            f'unpersisted staged keys; inspect price.bar merge_diagnostics log', err=True)
                if result.gaps_unresolved:
                    say(f'[PRICE] [{i}/{len(companies)}] WARNING {symbol}: '
                        f'{result.gaps_unresolved} interior XNYS slots remain without bars '
                        f'(may be legitimate no-trade/halts).', err=True)
                if args.export_dir and not args.dry_run:
                    path = export_company_csv(db, result.company_id, result.symbol, args.export_dir)
                    say(f'[PRICE] [{i}/{len(companies)}] CSV exported: {path}')
        finally:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)
    if args.source == 'massive' and not args.dry_run:
        say('[PRICE DIAG] TOTAL '
            + ' '.join(f'{k}={diagnostics_totals[k]}' for k in (
                'received', 'valid_unique', 'rejected_invalid', 'duplicates_in_response',
                'existing_keys', 'fresh_candidates', 'saved', 'sqlite_changes',
                'missing_after_merge', 'volume_defaulted',
                'trade_count_ignored', 'vwap_ignored'))
            + ' existing_by_source=' + (','.join(
                f'{k}:{v}' for k, v in sorted(diagnostics_sources.items())) or 'none')
            + ' rejected_by_reason=' + (','.join(
                f'{k}:{v}' for k, v in sorted(diagnostics_reasons.items()) if v) or 'none'))
        log_step('price.run', 'diagnostics_summary',
                 totals=dict(diagnostics_totals),
                 rejection_reasons=dict(diagnostics_reasons),
                 existing_by_source_feed=dict(diagnostics_sources))
    log_step('price.run', 'completed', companies=len(companies), bars_saved=total_saved,
             bars_received=total_received, failures=errors, gaps_unresolved=unresolved_gaps)
    say(f'[PRICE] Completed companies={len(companies)} bars_received={total_received} '
        f'bars_saved={total_saved} not_inserted={total_received-total_saved} '
        f'gaps_unresolved={unresolved_gaps} failures={errors}')
    return 1 if errors or (args.strict_gaps and unresolved_gaps) else 0


if __name__ == '__main__':
    raise SystemExit(run())
