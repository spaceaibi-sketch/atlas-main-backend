"""Opt-in re-extraction of older UNANALYSED main news correlations.

Never silently migrate old proven/analysed relationships. Used to eliminate
legacy generic-word links after an explicit operator-requested rebuild.
"""
from __future__ import annotations
import argparse
import json
import logging
from contextlib import closing
from sec_edgar.database import connect
from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import load_env, extraction_lock
from ai.client import openai_client
from .company_extraction import extract_companies, load_company_catalog
from .exposure_registry import load_exposures
from .identity_validation import load_ceos
from .matching import resolve_extracted_companies
from .store import ensure_schema, save_company_correlations

LOG=logging.getLogger(__name__)


def legacy_mains(db,limit):
    query='''SELECT n.* FROM atlas_articles n
        WHERE n.status IN ('extracted','connected') AND n.companies=''
        AND EXISTS(SELECT 1 FROM atlas_links r
                   WHERE r.news_id=n.news_id AND r.status='connected')
        AND NOT EXISTS(SELECT 1 FROM atlas_links r
                   WHERE r.news_id=n.news_id AND r.status='analysed')
        ORDER BY n.published_utc DESC LIMIT ?'''
    return [dict(row) for row in db.execute(query,(limit,))]


def rebuild(db, client, *, limit=100, batch_size=100, exposures=None,ceos=None):
    if not 1<=batch_size<=500 or limit<1:
        raise ValueError('Batch size must be 1..500; limit must be positive')
    company_catalog, catalog_names = load_company_catalog(db)
    count=0
    for offset in range(0,limit,batch_size):
        rows=legacy_mains(db,min(batch_size,limit-offset))
        if not rows:break
        failed={}
        proposed=extract_companies(client,rows,exposures=exposures,ceos=ceos,
                                   company_catalog=company_catalog,catalog_names=catalog_names,
                                   failed_articles=failed)
        for nid, reason in failed.items():
            LOG.warning('Historical extraction left unchanged: news_id=%s reason=%s',nid,reason)
        resolved=resolve_extracted_companies(db,proposed)
        with db:
            for row in rows:
                if row['news_id'] in proposed:
                    save_company_correlations(db,row['news_id'],resolved[row['news_id']])
        count+=len(proposed)
        if not proposed:
            break  # No successful article decisions; do not re-request forever.
    return count


def main(argv=None):
    parser=argparse.ArgumentParser(description='Re-evaluate old, unscored keyword-generated news links')
    parser.add_argument('--db',default=None)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--batch-size',type=int,default=100)
    parser.add_argument('--exposures-file',default=None)
    parser.add_argument('--ceos-file',default=None)
    parser.add_argument('--apply',action='store_true',help='Required to call AI and replace provisional old links')
    a=parser.parse_args(argv)
    load_env()
    path=a.db or database_path()
    from pathlib import Path
    with extraction_lock(Path(path).resolve().parent/'news_pipeline.lock') as locked:
        if not locked:raise RuntimeError('News pipeline currently in use')
        with closing(connect(path)) as db:
            ensure_schema(db)
            selected=legacy_mains(db,max(1,a.limit))
            if not a.apply:
                print(json.dumps({'eligible':len(selected),'would_reprocess':min(len(selected),max(0,a.limit)),
                       'applied':False,'note':'Old analysed relations are excluded. Pass --apply for paid AI calls.'},indent=2))
                return 0
            with openai_client() as client:
                done=rebuild(db,client,limit=a.limit,batch_size=a.batch_size,
                             exposures=load_exposures(a.exposures_file),
                             ceos=load_ceos(a.ceos_file))
            print(json.dumps({'reprocessed_main_articles':done,'applied':True},indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
