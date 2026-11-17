"""Standalone news AI scoring CLI: no RSS access, no per-article API calls."""
from __future__ import annotations
from shared.processing_log import log_step
from shared.config import integer as config_int, text as config_text
import argparse
from contextlib import closing
import json
import logging
from pathlib import Path
from ai.client import key_present, openai_client, api_key_setting
from news_pipeline.scoring import score_pending
from news_pipeline.store import ensure_schema,expire_pending
from sec_edgar.database import connect
from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import load_env,extraction_lock

LOG=logging.getLogger('news_ai')


def main(argv=None):
    p=argparse.ArgumentParser(description='Analyse linked main news articles through batched AI requests')
    p.add_argument('--limit',type=int,default=config_int('NEWS_SCORING_LIMIT',100,minimum=0),help='Relations per run; 0 means all pending')
    p.add_argument('--batch-size',type=int,default=config_int('NEWS_SCORING_BATCH_SIZE',20,minimum=2,maximum=100),help='Relations per API request, from 2 to 100')
    p.add_argument('--retry-errors',action='store_true',help='Include pending relations with old error details')
    p.add_argument('--dry-run',action='store_true',help='Count pending relations; no paid API calls')
    p.add_argument('--model',default=None)
    p.add_argument('--db',type=Path)
    args=p.parse_args(argv)
    if args.limit<0 or not 2 <= args.batch_size <= 100:
        p.error('Require --limit >= 0 and 2 <= --batch-size <= 100')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)-7s %(message)s')
    try:
        load_env()
        path=(args.db or database_path()).resolve()
        if not path.exists():
            raise FileNotFoundError(f'Database not found: {path}')
        with extraction_lock(path.parent/'news_ai_scoring.lock') as acquired:
            if not acquired:
                LOG.warning('Another news AI scorer is active; exiting (retry next hour)')
                return 75
            with closing(connect(path)) as db:
                ensure_schema(db)
                # Only run cleanup when the Phase 1 extractor is idle; never
                # invalidate a batch while its OpenAI response is in flight.
                stale_created_skipped=0
                if not args.dry_run:
                    with extraction_lock(path.parent/'news_pipeline.lock') as news_idle:
                        if news_idle:
                            stale_created_skipped=expire_pending(db)
                        else:
                            LOG.info('News extractor active; deferring stale-created cleanup')
                if not args.dry_run and not key_present():
                    raise ValueError(f'Missing {api_key_setting()} in .env')
                if args.dry_run:
                    result=score_pending(db,None,limit=args.limit,batch_size=args.batch_size,
                        retry_errors=args.retry_errors,dry_run=True,model=args.model)
                else:
                    with openai_client() as client:
                        result=score_pending(db,client,limit=args.limit,batch_size=args.batch_size,
                            retry_errors=args.retry_errors,model=args.model)
        result['stale_created_skipped']=stale_created_skipped
        log_step('news.scoring','cli_completed',**result)
        print(json.dumps(result,indent=2))
        return 1 if (result.get('errors') or result.get('postprocess_errors')) else 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted; completed news AI batches remain committed')
        return 130
    except Exception as exc:
        log_step('news.scoring','cli_failed',reason=str(exc)[:240],level='error')
        LOG.error('News AI stopped: %s: %s',type(exc).__name__,str(exc)[:240])
        return 1

if __name__=='__main__':
    raise SystemExit(main())
