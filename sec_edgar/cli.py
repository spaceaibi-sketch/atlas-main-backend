from shared.processing_log import log_step
from shared.config import integer as config_int, text as config_text
"""First extraction: SEC company/ticker directory + new 8-K/10-K/10-Q filings."""
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from .database import connect
from shared.stale_created import skip_created
from .http import SecClient,shared_rate_state_path
from .collector import Collector
from .companies import load_sec_company_json,refresh_companies
from .settings import database_path
from .windows_launcher import load_env,extraction_lock

LOG=logging.getLogger(__name__)

def main(argv=None):
    p=argparse.ArgumentParser(description='SEC metadata -> companies and forms tables')
    p.add_argument('--db',type=Path)
    p.add_argument('--init-db',action='store_true')
    p.add_argument('--import-atom',type=Path,help='Offline SEC Atom fixture')
    p.add_argument('--companies-file',type=Path,help='Offline SEC company JSON')
    p.add_argument('--hours', type=int, default=None, help='Deprecated; ignored. All Atom pages are considered.')
    p.add_argument('--max-atom-pages',type=int,default=config_int('SEC_MAX_ATOM_PAGES',100,minimum=1))
    p.add_argument('--now',help='Deprecated; ignored. SEC filing dates are not filtered.')
    p.add_argument('--verbose',action='store_true')
    args=p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,format='%(asctime)s %(levelname)-7s %(message)s')
    if args.max_atom_pages<=0:p.error('--max-atom-pages must be positive')
    if args.hours is not None or args.now is not None:
        LOG.warning('--hours/--now are deprecated and ignored; all available SEC Atom entries are considered')
    try:
        load_env();path=(args.db or database_path()).resolve()
        with extraction_lock(path.parent/'extract_recent_filings.lock') as acquired:
            if not acquired:LOG.warning('Another filing collection is active');return 75
            with connect(path) as db:
                if args.init_db:
                    seed=Path(__file__).resolve().parents[1]/'examples'/'sec_company_tickers_exchange.json'
                    count=0
                    if seed.exists() and db.execute('SELECT COUNT(*) FROM companies').fetchone()[0]==0:
                        count=load_sec_company_json(db,seed.read_bytes())
                    print(f'SQLite ready: {path} (SEC tables ready; {count} SEC companies seeded)');return 0
                # Expire stale created filings before any SEC network I/O.
                # Never mark a filing skipped while the report parser holds it.
                with extraction_lock(path.parent/'extract_forms_items.lock') as forms_idle:
                    stale_created_skipped=(
                        skip_created(db,sources=('filing',))['filing']
                        if forms_idle else 0)
                offline=bool(args.import_atom or args.companies_file)
                client=None if offline else SecClient(os.getenv('SEC_USER_AGENT',''),rate_state=shared_rate_state_path(path))
                try:
                    company_count=0
                    if args.companies_file:company_count=load_sec_company_json(db,args.companies_file.read_bytes())
                    elif client:
                        try:company_count=refresh_companies(db,client,path)
                        except Exception as exc:LOG.warning('Company directory not updated: %s: %s',type(exc).__name__,str(exc)[:200])
                    collector=Collector(db,client,max_pages=args.max_atom_pages,db_path=path)
                    if args.import_atom:
                        payload=args.import_atom.read_bytes()
                        collector.import_atom(payload,origin_url=args.import_atom.resolve().as_uri())
                    elif client:
                        collector.live_atom()
                    stats=collector.stats
                    stats['stale_created_skipped']=stale_created_skipped
                    stats['companies_directory_loaded']=company_count
                    stats['requests']=client.request_count if client else 0
                finally:
                    if client:client.close()
        log_step('sec.atom','run_completed',**stats)
        print(json.dumps(stats,indent=2))
        return 1 if stats['errors'] else 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted by user. Previously inserted company/form records are retained.')
        return 130
    except Exception as exc:
        log_step('sec.atom','run_failed',reason=str(exc).replace('\n',' ')[:250],level='error')
        LOG.error('Recent filings extractor stopped: %s: %s',type(exc).__name__,str(exc).replace('\n',' ')[:350]);return 1
if __name__=='__main__':sys.exit(main())
