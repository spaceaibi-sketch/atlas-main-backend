"AI-only batch command; does not contact SEC."
from __future__ import annotations
from shared.processing_log import log_step
from shared.config import integer as config_int, text as config_text
import argparse
import json
import logging
import os
from pathlib import Path
from sec_edgar.database import connect
from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import extraction_lock, load_env
from shared.stale_created import skip_created
from .client import key_present, openai_client, api_key_setting
from .worker import process_pending_ai

LOG=logging.getLogger(__name__)


def run_ai(db, db_path, *, limit=25, retry_errors=False, reanalyse_zero_prices=False, model=None, dry_run=False, batch_size=20):
    log_step('ai.item','run_started',db_path=str(db_path),limit=limit,retry_errors=retry_errors,reanalyse_zero_prices=reanalyse_zero_prices,model=model,dry_run=dry_run,batch_size=batch_size)
    with extraction_lock(db_path.parent/'analyse_items.lock') as acquired:
        if not acquired:
            log_step('ai.item','lock_skipped',reason='Another AI analysis job is running')
            LOG.warning('Another AI analysis job is running; skipping')
            return {'skipped_concurrent': True}
        stale_created_skipped = 0
        if not dry_run:
            # The SEC report parser can insert/rewrite Items while working.
            # Do not expire records under a parser with an in-flight update.
            with extraction_lock(db_path.parent/'extract_forms_items.lock') as forms_idle:
                if forms_idle:
                    stale_created_skipped = skip_created(db, sources=('item',))['item']
                    if stale_created_skipped:
                        LOG.info('SEC Items created at least 24 hours ago skipped=%d',
                                 stale_created_skipped)
                else:
                    LOG.info('SEC report parser active; deferring stale Item cleanup')
        if not dry_run and not key_present():
            raise ValueError(f'Missing {api_key_setting()} in .env')
        client = None if dry_run else openai_client()
        try:
            result = process_pending_ai(db,client,limit=limit,retry_errors=retry_errors,reanalyse_zero_prices=reanalyse_zero_prices,model=model,dry_run=dry_run,batch_size=batch_size)
            result['stale_created_skipped'] = stale_created_skipped
            return result
        finally:
            if client is not None:client.close()


def main(argv=None):
    p=argparse.ArgumentParser(description='Analyze successfully parsed SEC Items using the configured AI API')
    p.add_argument('--db',type=Path)
    p.add_argument('--limit',type=int,default=config_int('SEC_AI_LIMIT',25,minimum=0),help='Maximum Items per run; 0 means all (paid calls)')
    p.add_argument('--retry-errors',action='store_true',help='Retry failed AI analyses')
    p.add_argument('--batch-size',type=int,default=config_int('SEC_AI_BATCH_SIZE',20,minimum=1,maximum=25),help='SEC Items per AI request (1-25, default 20)')
    p.add_argument('--reanalyse-zero-prices',action='store_true',
                   help='Opt in to paid reanalysis of already analysed Items with a 0%% price estimate')
    p.add_argument('--dry-run',action='store_true',help='List counts without using API or changing SQLite')
    p.add_argument('--model',default=None,help='Model override; default selected by AI_PROVIDER in .env')
    args=p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)-7s %(message)s')
    if args.limit<0:p.error('--limit must be >=0')
    if not 1 <= args.batch_size <= 25:p.error('--batch-size must be 1..25')
    try:
        load_env()
        db_path=(args.db or database_path()).resolve()
        if not db_path.exists():raise FileNotFoundError(f'Database not found: {db_path}')
        with connect(db_path) as db:
            stats=run_ai(db,db_path,limit=args.limit,retry_errors=args.retry_errors,
                         reanalyse_zero_prices=args.reanalyse_zero_prices,
                         model=args.model,dry_run=args.dry_run,batch_size=args.batch_size)
        print(json.dumps(stats,indent=2))
        # A nonblocking lock skip is not a successful scoring run.  Returning
        # EX_TEMPFAIL keeps cron's last_success stamp unchanged, so a later
        # hour can retry once the genuine holder releases the advisory lock.
        if stats.get('skipped_concurrent'):
            return 75
        return 1 if (stats.get('errors') or stats.get('postprocess_errors')) else 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted; committed AI results remain saved')
        return 130
    except Exception as exc:
        LOG.error('AI analysis stopped: %s: %s',type(exc).__name__,str(exc)[:250])
        return 1

if __name__=='__main__':
    raise SystemExit(main())
