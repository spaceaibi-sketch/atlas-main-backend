from shared.processing_log import log_step
from shared.config import integer as config_int, text as config_text
"""Separate report downloader and Item parser, driven only by SQLite forms rows."""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .database import connect,store_document,store_items,set_form_error
from shared.stale_created import skip_created
from .http import SecClient,shared_rate_state_path
from .form_parser import parse_document
from .reports import (report_dir, primary_document_url, save_report,
                      complete_submission_url, primary_document_from_submission,
                      sec_access_problem)
from .settings import database_path
from .raw_archive import archive_bytes, filing_subject, accession_from_url
from .windows_launcher import load_env,extraction_lock

LOG=logging.getLogger(__name__)

def short_error(exc):return (type(exc).__name__+': '+str(exc).replace('\n',' ').strip())[:350]

def process_forms(db, client, db_path, *, limit=500, stale_created_skipped=0):
    """Stage 2: use SQLite only, download unlinked reports, then parse/save Items.

    Reports already on disk are reused for parsing retries. Each successfully
    committed accession is skipped on subsequent runs.
    """
    # A parsed flag alone is not sufficient: recover records whose Item rows
    # are missing (for example after a partial legacy migration). Successful
    # forms with their saved report and Items are never selected again.
    needs_work = """status != 'skipped' AND (status != 'parsed' OR file_guid IS NULL OR file_name IS NULL
                   OR primary_url IS NULL OR NOT EXISTS
                   (SELECT 1 FROM atlas_items i WHERE i.form_id=atlas_filings.id
                    AND i.file_guid=atlas_filings.file_guid))"""
    query = f"""SELECT id, form_type, index_url, primary_url, file_guid,
                        file_name, status FROM atlas_filings
                 WHERE {needs_work}
                 -- Broken filings stay eligible but cannot monopolize a limited
                 -- batch ahead of new, unattempted filings.
                 ORDER BY CASE WHEN COALESCE(error, '') != '' THEN 1 ELSE 0 END,
                          filed_date DESC, id DESC"""
    pending = db.execute(f'SELECT COUNT(*) FROM atlas_filings WHERE {needs_work}').fetchone()[0]
    total = db.execute('SELECT COUNT(*) FROM atlas_filings').fetchone()[0]
    rows = db.execute(query + (' LIMIT ?' if limit > 0 else ''),
                      (limit,) if limit > 0 else ()).fetchall()
    already_skipped = db.execute(
        "SELECT COUNT(*) FROM raw_items WHERE source_type='filing' AND status='skipped'"
    ).fetchone()[0]
    stats = {'total_forms': total, 'skipped_complete': total - pending - already_skipped,
             'skipped_status': already_skipped,
             'stale_created_skipped': stale_created_skipped,
             'pending_start': pending, 'selected': len(rows), 'downloaded': 0,
             'reused_local': 0, 'parsed': 0, 'items_saved': 0,
             'errors': 0, 'remaining': pending, 'requests': 0,
             'stopped_for_sec_access': False, 'error_record_failures': 0}
    LOG.info('REPORTS total=%d skipped_complete=%d pending=%d selected=%d SEC pacing=4 requests/sec',
             total, total - pending, pending, len(rows))
    for n, row in enumerate(rows, 1):
        form_id, form = row['id'], row['form_type']
        # Even malformed row metadata must be isolated to this filing.
        acc, path, saved = '<invalid accession>', None, False
        stage = 'identify SEC accession'
        try:
            acc = accession_from_url(row['index_url'])
            guid, name = row['file_guid'], row['file_name']
            path = (report_dir(db_path) / name) if name else None
            log_step('sec.form','started',form_id=form_id,form=form,accession=acc,index_url=row['index_url'],position=n,total=len(rows))
            stage = 'load existing report'
            if not guid or not path or not path.is_file() or not row['primary_url']:
                # index_url points to a document LISTING, not the primary report.
                stage = 'fetch SEC filing detail'
                log_step('sec.form','download_index',form_id=form_id,url=row['index_url'])
                page = client.get(row['index_url'])
                archive_bytes(db_path, service='sec_filings',
                              subject=filing_subject(row['index_url']), kind='index',
                              url=row['index_url'],payload=page,extension='.htm')
                stage = 'check SEC detail response'
                access_problem = sec_access_problem(page)
                if access_problem:
                    raise ValueError(access_problem)
                stage = 'find primary document link'
                try:
                    primary_url = primary_document_url(page, row['index_url'], form)
                except ValueError as detail_error:
                    # Some SEC filings have a nonstandard/missing document table.
                    # The official SEC complete-submission SGML file lists each
                    # document's real TYPE and FILENAME, including the primary.
                    stage = 'read SEC complete-submission headers'
                    submission = client.get(complete_submission_url(row['index_url'], acc))
                    archive_bytes(db_path,service='sec_filings',
                                  subject=filing_subject(row['index_url']), kind='submission',
                                  url=complete_submission_url(row['index_url'], acc),
                                  payload=submission,extension='.txt')
                    access_problem = sec_access_problem(submission)
                    if access_problem:
                        raise ValueError(access_problem)
                    stage = 'find primary document in SEC submission headers'
                    try:
                        primary_url = primary_document_from_submission(submission, row['index_url'], form)
                    except ValueError as submission_error:
                        raise ValueError(f'{detail_error}; fallback: {submission_error}') from submission_error
                    LOG.info('[%d/%d] SEC INDEX FALLBACK %-4s %s primary=%s',
                             n, len(rows), form, acc, primary_url.rsplit('/', 1)[-1])
                stage = 'download primary report'
                log_step('sec.form','download_primary',form_id=form_id,url=primary_url)
                content = client.get(primary_url)
                archive_bytes(db_path,service='sec_filings',
                              subject=filing_subject(row['index_url']),kind='primary',
                              url=primary_url,payload=content)
                stage = 'save UUID report file'
                guid, name = save_report(db_path, content, primary_url)
                stage = 'link report to SQLite'
                store_document(db, form_id, url=primary_url, guid=guid, filename=name)
                path = report_dir(db_path) / name
                stats['downloaded'] += 1
                saved = True
                LOG.info('[%d/%d] SAVED %-4s %s file=%s', n, len(rows), form, acc, name)
            else:
                content = path.read_bytes()
                # The original bytes may predate this release. When reparsing a
                # retained local report, also place them in the per-filing archive.
                archive_bytes(db_path,service='sec_filings',
                              subject=filing_subject(row['index_url']),kind='primary',
                              url=row['primary_url'],payload=content,
                              metadata={'reused_local_report': True})
                stats['reused_local'] += 1
                log_step('sec.form','reused_local',form_id=form_id,document_guid=guid,filename=name)
            stage = 'parse Item text'
            parsed = parse_document(content, form, suffix=path.suffix)
            log_step('sec.form','extracted_sections',form_id=form_id,count=len(parsed),item_types=[p.item_code for p in parsed])
            if not parsed:
                raise ValueError('No Item sections found in primary report')
            stage = 'save Items to SQLite'
            store_items(db, form_id, guid, parsed)
            kept = db.execute('SELECT COUNT(*) FROM atlas_items WHERE form_id=?', (form_id,)).fetchone()[0]
            stats['parsed'] += 1
            stats['items_saved'] += kept
            LOG.info('[%d/%d] PARSED %-4s %s items=%d', n, len(rows), form, acc, kept)
        except Exception as exc:
            stats['errors'] += 1
            err = f'{stage}: {short_error(exc)}'
            log_step('sec.form','stage_error',form_id=form_id,error_stage=stage,reason=err,level='error')
            try:
                set_form_error(db, form_id, err, leave_extracted=bool(
                    (row['file_guid'] or saved) and path and path.is_file()))
            except Exception:
                # Even failure to persist an error must not discard successful
                # filings later in this batch. Record the secondary failure.
                stats['error_record_failures'] += 1
                LOG.exception('Could not store the error for filing %s (id=%s)', acc, form_id)
            LOG.error('[%d/%d] FAILED %-4s %s: %s', n, len(rows), form, acc, err)
            # A malformed document is not a batch-level failure: continue with
            # every other selected accession. Failed files remain observable and
            # retryable after a parser fix; do not silently discard their data.
            if '403 Client Error' not in err and '429 Client Error' not in err:
                log_step('sec.form', 'isolated_filing_error', form_id=form_id,
                         accession=acc, remaining_in_batch=len(rows)-n,
                         reason=err, level='warning')
            if '403 Client Error' in err or '429 Client Error' in err:
                # A 403/429 is an upstream access problem, not a malformed
                # individual filing: pause the batch instead of hammering SEC.
                stats['stopped_for_sec_access'] = True
                LOG.error('SEC denied/throttled requests; stopping this run')
                break
    stats['remaining'] = db.execute(f'SELECT COUNT(*) FROM atlas_filings WHERE {needs_work}').fetchone()[0]
    stats['requests'] = getattr(client, 'request_count', 0)
    stats['batch_status'] = ('interrupted_sec_access' if stats['stopped_for_sec_access']
                             else 'completed_with_item_errors' if stats['errors']
                             else 'completed')
    LOG.info('REPORTS complete: downloaded=%d parsed=%d failed=%d remaining=%d requests=%d',
             stats['downloaded'], stats['parsed'], stats['errors'],
             stats['remaining'], stats['requests'])
    log_step('sec.form','run_completed',**stats)
    return stats

def main(argv=None):
    p=argparse.ArgumentParser(description='Download missing primary reports and parse Items for forms in SQLite')
    p.add_argument('--db',type=Path)
    p.add_argument('--limit',type=int,default=config_int('SEC_FORMS_LIMIT',500,minimum=0),help='Maximum forms per run, 0 = all')
    p.add_argument('--verbose',action='store_true')
    args=p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,format='%(asctime)s %(levelname)-7s %(message)s')
    if args.limit<0:p.error('--limit must be >=0')
    try:
        load_env();path=(args.db or database_path()).resolve()
        if not path.exists():raise FileNotFoundError(f'SQLite missing: {path}. Run extract_recent_filings.bat first.')
        with extraction_lock(path.parent/'extract_forms_items.lock') as acquired:
            if not acquired:LOG.warning('Another report process is active');return 75
            with connect(path) as db:
                stale_created_skipped=skip_created(db,sources=('filing',))['filing']
                if stale_created_skipped:
                    LOG.info('SEC filings created at least 24 hours ago skipped=%d',stale_created_skipped)
                client=SecClient(os.environ.get('SEC_USER_AGENT',''),rate_state=shared_rate_state_path(path))
                try:stats=process_forms(db,client,path,limit=args.limit,stale_created_skipped=stale_created_skipped)
                finally:client.close()
        print(json.dumps(stats,indent=2))
        # The per-filing try/except above has already isolated and recorded
        # individual failures. Do not fail the entire scheduled batch merely
        # because one (or more) reports could not be parsed. All still-pending
        # filings remain eligible for the next scheduled extraction.
        if stats['errors'] and not stats['stopped_for_sec_access']:
            LOG.warning('REPORTS completed with %d isolated filing error(s); '
                        'other filings remain committed; pending forms retry on the next run',
                        stats['errors'])
        # An SEC access block is a run-level outage and must be retried as such.
        return 1 if stats['stopped_for_sec_access'] else 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted by user; already downloaded files and committed Items remain saved.')
        return 130
    except Exception as exc:
        LOG.error('Forms and Items extractor stopped: %s',short_error(exc));return 1

if __name__=='__main__':sys.exit(main())
