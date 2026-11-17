"""Massive downloader: parallel HTTP, crash-recoverable disk queue, one SQLite writer.

Price responses are durably staged before the writer sees them. A writer batch
may be replayed after a crash or lock; PRIMARY KEY + ON CONFLICT DO NOTHING
makes replay idempotent. Only committed and verified batches are acknowledged.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
from datetime import datetime
from collections import deque
import sqlite3
from threading import local
import time

from shared.config import integer as config_int, number as config_num
from shared.processing_log import log_step
from sec_edgar.database import connect
from .collector import PricePeriod, collect_company
from .calendar import STEP, safe_end
from .storage import missing_regular_slots
from .durable_writer import MassivePriceSpool, SpoolError
from .massive import MassiveClient, MassiveError
from .storage import export_company_csv


def _is_busy(exc):
    return (isinstance(exc, sqlite3.OperationalError) and
            any(x in str(exc).lower() for x in ('locked', 'busy')))


def _diagnostic_summary(diags, totals, reasons, sources, say, symbol, company_id):
    for diag in diags:
        totals.update({field: int(diag.get(field, 0)) for field in (
            'received', 'valid_unique', 'rejected_invalid', 'duplicates_in_response',
            'existing_keys', 'fresh_candidates', 'saved', 'sqlite_changes',
            'missing_after_merge', 'volume_defaulted', 'trade_count_ignored',
            'vwap_ignored')})
        reasons.update(diag['rejection_reasons'])
        sources.update(diag['existing_by_source_feed'])
        rejected = ','.join(f'{k}:{v}' for k, v in diag['rejection_reasons'].items() if v) or 'none'
        origins = ','.join(f'{k}:{v}' for k, v in diag['existing_by_source_feed'].items()) or 'none'
        say(f'[PRICE DIAG] {symbol} company={company_id} batch={diag["batch"]} '
            f'received={diag["received"]} valid_unique={diag["valid_unique"]} '
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
            f'existing_by_source={origins} rejected_by_reason={rejected} '
            f'volume_defaulted={diag["volume_defaulted"]} '
            f'trade_count_ignored={diag["trade_count_ignored"]} '
            f'vwap_ignored={diag["vwap_ignored"]}')


def run_massive_queued(*, db_path, companies, period, now, client, key, limiter,
                       massive_factory, threads, no_gap_repair, strict_gaps,
                       export_dir, say):
    """Return nonzero when any planned/fetched work remains uncommitted."""
    spool = MassivePriceSpool(db_path)
    with spool.exclusive_run():
        recovery = spool.pending_ids()
        # Persist ALL current selected companies before any network requests.
        # The original lookback survives SIGKILL, timeout and interrupted runs.
        planned = [spool.plan(row, period, now)['id'] for row in companies]
        total_jobs = len(recovery) + len(planned)
        say(f'[PRICE QUEUE] single_writer=1 fetch_threads={threads} '
            f'recovered={len(recovery)} planned={len(planned)} '
            f'spool={spool.path}')
        job_ids = deque(recovery + planned)
        known_ids = set(job_ids)
        downloads = {}
        writes = {}
        fetch_state = local()
        writer_state = local()
        max_inflight = max(2, threads * 2)
        errors = 0
        received = saved = gaps = completed = 0
        diag_totals, diag_reasons, diag_sources = Counter(), Counter(), Counter()
        fatal_lock = False
        attempts = config_int('ATLAS_PRICE_LOCK_RETRY_ATTEMPTS', 8, minimum=1, maximum=30)
        backoff = config_num('ATLAS_PRICE_LOCK_RETRY_SECONDS', 2, minimum=0, maximum=60)

        def fetch_spooled(job_id):
            job = spool.load(job_id)
            worker = getattr(fetch_state, 'client', None)
            if worker is None:
                worker = (massive_factory(key, limiter=limiter)
                          if massive_factory is MassiveClient else massive_factory(key))
                fetch_state.client = worker
            bars = worker.bars(job['symbol'], datetime.fromisoformat(job['start']),
                               datetime.fromisoformat(job['end']))
            spool.store_fetched(job_id, bars)
            return job_id

        def writer_spooled(job_id):
            job = spool.load(job_id)
            if job['state'] != 'fetched':
                raise SpoolError(f'Writer received an un-fetched price batch: {job_id}')
            row = {'id': job['company_id'], 'ticker': job['symbol'],
                   'last_activity': job.get('latest_activity', '')}
            request_period = PricePeriod(datetime.fromisoformat(job['start']),
                                         datetime.fromisoformat(job['end']))
            request_now = datetime.fromisoformat(job['now'])
            for attempt in range(1, attempts + 1):
                try:
                    db = getattr(writer_state, 'db', None)
                    if db is None:
                        db = connect(db_path)
                        writer_state.db = db
                    # No HTTP call on the DB thread: even gap-repair fetches
                    # must go through the durable downloader queue.
                    result = collect_company(db, client, row, request_period,
                                             now=request_now, prefetched=job['bars'],
                                             repair_gaps=False,
                                             verify_gaps=strict_gaps)
                    # This is a hard failure, not just a warning: never ACK
                    # a batch for which SQLite did not retain all valid keys.
                    if any(diag.get('merge_mismatch') or diag.get('missing_after_merge')
                           for diag in result.diagnostic_batches):
                        raise SpoolError(f'SQLite price verification failed for {job["symbol"]}')
                    repair_id = None
                    if job.get('kind') == 'initial' and not no_gap_repair:
                        checked_end = min(request_period.end, safe_end(request_now))
                        if checked_end > request_period.start:
                            missing = missing_regular_slots(
                                db, result.company_id, request_period.start, checked_end)
                            if missing:
                                repair_id = spool.ensure_repair(
                                    job, missing[0], missing[-1] + STEP)
                                result.gaps_unresolved = 0  # count after repair
                    elif job.get('kind') == 'gap_repair':
                        for diagnostic in result.diagnostic_batches:
                            diagnostic['batch'] = 'gap_repair'
                        original_start = datetime.fromisoformat(job['original_start'])
                        original_end = datetime.fromisoformat(job['original_end'])
                        checked_end = min(original_end, safe_end(request_now))
                        result.gaps_unresolved = (len(missing_regular_slots(
                            db, result.company_id, original_start, checked_end))
                            if checked_end > original_start else 0)
                    if export_dir is not None:
                        export_company_csv(db, result.company_id, result.symbol, export_dir)
                    # Atomic commit is completed inside collect_company.
                    # A gap-repair plan, if needed, is fsynced BEFORE this ACK.
                    spool.complete(job_id)
                    return result, repair_id
                except sqlite3.OperationalError as exc:
                    # Reopen after every SQLite operational failure so a stale
                    # read/write transaction cannot keep any lock alive.
                    conn = getattr(writer_state, 'db', None)
                    if conn is not None:
                        try:
                            conn.rollback()
                        finally:
                            conn.close()
                            writer_state.db = None
                    if not _is_busy(exc) or attempt >= attempts:
                        raise
                    say(f'[PRICE QUEUE] RETRY {job["symbol"]} database locked '
                        f'attempt={attempt}/{attempts}; persisted batch retained', err=True)
                    time.sleep(min(30, backoff * 2 ** min(attempt - 1, 4)))

        def writer_close():
            conn = getattr(writer_state, 'db', None)
            if conn is not None:
                conn.close()
                writer_state.db = None

        def dispatch(job_id, number, pool):
            job = spool.load(job_id)
            if job['state'] == 'fetched':
                writes[pool.submit(writer_spooled, job_id)] = (number, job['symbol'])
            else:
                downloads[fetch_pool.submit(fetch_spooled, job_id)] = (job_id, number, job['symbol'])

        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix='massive-fetch') as fetch_pool:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='massive-db-writer') as writer_pool:
                next_number = 0
                try:
                    while True:
                        while not fatal_lock and len(downloads) + len(writes) < max_inflight:
                            try:
                                job_id = job_ids.popleft()
                            except IndexError:
                                break
                            next_number += 1
                            try:
                                dispatch(job_id, next_number, writer_pool)
                            except Exception as exc:
                                errors += 1
                                say(f'[PRICE QUEUE] ERROR reading persisted batch {job_id}: {exc}', err=True)
                                # The file is deliberately left untouched.
                        if not downloads and not writes:
                            break
                        done, _ = wait(tuple(downloads) + tuple(writes), return_when=FIRST_COMPLETED)
                        for future in done:
                            if future in downloads:
                                job_id, number, symbol = downloads.pop(future)
                                try:
                                    future.result()
                                except Exception as exc:
                                    errors += 1
                                    say(f'[PRICE] [{number}/{total_jobs}] ERROR fetching {symbol}: '
                                        f'{type(exc).__name__}: {exc}; job retained for retry', err=True)
                                else:
                                    # On a terminal DB lock, keep fetched data on
                                    # disk instead of queueing more doomed writes.
                                    if not fatal_lock:
                                        writes[writer_pool.submit(writer_spooled, job_id)] = (number, symbol)
                            else:
                                number, symbol = writes.pop(future)
                                try:
                                    result, repair_id = future.result()
                                except Exception as exc:
                                    errors += 1
                                    say(f'[PRICE] [{number}/{total_jobs}] ERROR saving {symbol}: '
                                        f'{type(exc).__name__}: {exc}; job retained for retry', err=True)
                                    if _is_busy(exc):
                                        fatal_lock = True
                                else:
                                    completed += 1
                                    if repair_id is not None and repair_id not in known_ids:
                                        job_ids.append(repair_id)
                                        known_ids.add(repair_id)
                                        total_jobs += 1
                                        say(f'[PRICE QUEUE] {symbol} queued durable gap repair')
                                    received += result.received
                                    saved += result.saved
                                    gaps += result.gaps_unresolved
                                    say(f'[PRICE] [{number}/{total_jobs}] DONE {result.symbol} '
                                        f'bars_received={result.received} bars_saved={result.saved} '
                                        f'not_inserted={result.received-result.saved} '
                                        f'gaps_unresolved={result.gaps_unresolved}')
                                    _diagnostic_summary(result.diagnostic_batches, diag_totals,
                                                        diag_reasons, diag_sources, say,
                                                        result.symbol, result.company_id)
                                    if result.gaps_unresolved:
                                        say(f'[PRICE] WARNING {symbol}: '
                                            f'{result.gaps_unresolved} session gaps still unresolved', err=True)
                finally:
                    # Thread-pool shutdown waits for dispatched fetches/writes.
                    # Their completed spool files survive even if not consumed.
                    writer_pool.submit(writer_close).result()
        remaining = len(spool.pending_ids())
        say('[PRICE DIAG] TOTAL '
            + ' '.join(f'{k}={diag_totals[k]}' for k in (
                'received', 'valid_unique', 'rejected_invalid', 'duplicates_in_response',
                'existing_keys', 'fresh_candidates', 'saved', 'sqlite_changes',
                'missing_after_merge', 'volume_defaulted', 'trade_count_ignored',
                'vwap_ignored'))
            + ' existing_by_source=' + (','.join(f'{k}:{v}' for k, v in sorted(diag_sources.items())) or 'none')
            + ' rejected_by_reason=' + (','.join(f'{k}:{v}' for k, v in sorted(diag_reasons.items()) if v) or 'none'))
        say(f'[PRICE QUEUE] completed={completed} pending_durable={remaining} '
            f'recovered_at_start={len(recovery)} failed={errors}')
        log_step('price.run', 'diagnostics_summary', totals=dict(diag_totals),
                 rejection_reasons=dict(diag_reasons),
                 existing_by_source_feed=dict(diag_sources), spool_pending=remaining)
        log_step('price.run', 'completed', companies=len(companies),
                 recovered=len(recovery), bars_saved=saved, bars_received=received,
                 failures=errors, gaps_unresolved=gaps, spool_pending=remaining)
        say(f'[PRICE] Completed companies={len(companies)} bars_received={received} '
            f'bars_saved={saved} not_inserted={received-saved} '
            f'gaps_unresolved={gaps} failures={errors} pending_durable={remaining}')
        return 1 if errors or remaining or (strict_gaps and gaps) else 0
