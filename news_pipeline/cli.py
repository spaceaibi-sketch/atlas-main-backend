"""One-command news run: RSS ingest + one bulk AI event-grouping call per <=500 stories."""
from __future__ import annotations
from shared.processing_log import log_step
from shared.config import integer as config_int, text as config_text
import argparse
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
from sec_edgar.database import connect
from sec_edgar.raw_archive import archive_bytes
from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import load_env,extraction_lock
from ai.client import openai_client
from .feeds import load_sources,parse_feed,SOURCE_CSV
from .http import FeedClient
from .store import (ensure_schema,add_story,pending_news,previous_main_news,save_batch_groups,
                    expire_pending,save_extracted_companies,save_company_correlations,news_statistics)
from .matching import resolve_extracted_companies
from .company_extraction import extract_companies, load_company_catalog
from .exposure_registry import load_exposures
from .identity_validation import load_ceos
from .batch_filter import analyze_batch,MAX_BATCH_SIZE,REFERENCE_LIMIT
from .time_window import publication_status,utc_now,ensure_utc_now
from .articles import article_for_news

LOG=logging.getLogger('news_pipeline')

def process_news(conn,client, sources, *, max_entries=0, max_new=0, now=None):
    now=ensure_utc_now(now)
    stats={'sources':0,'feed_errors':0,'read':0,'eligible':0,'inserted':0,
           'duplicates':0,'invalid':0,'outside_3h':0,'missing_time':0,'future':0}
    archive_path=conn.execute('PRAGMA database_list').fetchone()[2]
    for source in sources:
        if max_new>0 and stats['inserted']>=max_new:break
        try:
            payload=client.get(source['url'])
            archive_bytes(archive_path,service='news_feeds',subject=source['name'],
                          kind='rss_xml',url=source['url'],payload=payload,extension='.xml')
            articles=parse_feed(payload,source,max_entries=0)
            stats['sources']+=1;stats['read']+=len(articles)
            eligible=[]
            for story in articles:
                status=publication_status(story.published_utc,now)
                log_step('news.feed','classified',source=source['name'],title=story.title,url=story.url,published_utc=story.published_utc,decision=status)
                if status=='fresh':eligible.append(story)
                else:stats[status if status!='outside_window' else 'outside_3h']+=1
            eligible.sort(key=lambda story:story.published_utc,reverse=True)
            if max_entries>0:eligible=eligible[:max_entries]
            stats['eligible']+=len(eligible)
            for story in eligible:
                if max_new>0 and stats['inserted']>=max_new:break
                outcome,_=add_story(conn,story)
                if outcome=='inserted':stats['inserted']+=1
                elif outcome=='invalid':stats['invalid']+=1
                else:stats['duplicates']+=1
            LOG.info('RSS %-37s entries=%d last_3h=%d inserted_so_far=%d',
                     source['name'][:37],len(articles),len(eligible),stats['inserted'])
        except Exception as exc:
            log_step('news.feed','source_error',source=source['name'],reason=str(exc)[:250],level='error')
            stats['feed_errors']+=1;LOG.error('RSS %s: %s: %s',source['name'],type(exc).__name__,str(exc)[:180])
    log_step('news.feed','run_completed',**stats)
    return stats


def process_ai(conn,client, *, limit=0,retry_errors=False,article_client=None,now=None,batch_size=500,exposures=None,ceos=None,request_recorder=None,preexpired=None):
    """Two batch AI calls: event dedup, then ID|Company|Reason extraction.

    Deterministic resolution runs strictly after contextual extraction.
    Only uniquely resolved SEC identities become candidate relationships.
    """
    if not 2 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError('News batch size must be in 2..500')
    now=ensure_utc_now(now)
    expired=expire_pending(conn,now) if preexpired is None else preexpired
    rows=pending_news(conn,limit=limit,retry_errors=retry_errors,now=now)
    totals={'queued':len(rows),'batches':0,'main':0,'duplicates':0,'groups':0,
            'deferred':0,'errors':0,'stale_skipped':expired,
            'extracted_companies':0,'resolved_companies':0,'relations':0,'analysed':0,'skipped':0,
            'ai_news_with_companies':0,'connected_news':0,'connected_relations':0,
            'no_company_skipped':0,'catalog_unmatched':0,
            'failed_article_decisions':0,'partial_batches':0}
    company_catalog, catalog_names = load_company_catalog(conn) if rows else ('', {})
    offset=0
    while offset<len(rows):
        references=previous_main_news(conn,now=now,limit=min(REFERENCE_LIMIT,batch_size//5),
                                     exclude={row['news_id'] for row in rows[offset:]})
        count=min(batch_size-len(references),len(rows)-offset)
        pending=rows[offset:offset+count]
        if len(pending)+len(references)<2:
            totals['deferred']+=len(pending)
            LOG.info('AI BATCH: one isolated article deferred until another arrives (no single-article API calls)')
            break
        # Optional site fetch uses HTTP per article; OpenAI is still called once per batch.
        if article_client:
            for story in pending:
                if not story.get('article_text'):
                    try:
                        content=article_for_news(article_client,story,db_path=conn.execute('PRAGMA database_list').fetchone()[2])
                        if content:
                            with conn:
                                conn.execute("UPDATE raw_items SET content=? WHERE source_type='news' AND source_key=?",
                                             (content,story['news_id']))
                            story['article_text']=content
                    except Exception as exc:
                        LOG.info('Article fallback to RSS: %s: %s',type(exc).__name__,str(exc)[:100])
        batch_number = totals['batches'] + totals['errors'] + 1
        log_step('news.ai','batch_started',batch=batch_number,new_news_ids=[r['news_id'] for r in pending],reference_news_ids=[r['news_id'] for r in references])
        recorder = (None if request_recorder is None else
                    lambda stage, request: request_recorder(batch_number, stage, request))
        try:
            failed_articles={}
            mains,duplicates,groups=analyze_batch(
                client,pending,references,request_recorder=recorder,
                failed_articles=failed_articles)
            main_lookup={r['news_id']:r for r in pending}
            main_rows=[main_lookup[nid] for nid in mains]
            # ONLY AI contextual extraction proposes company identities.
            unmatched_in_batch=[]
            extracted=extract_companies(client,main_rows,exposures=exposures,ceos=ceos,
                                        request_recorder=recorder, company_catalog=company_catalog,
                                        catalog_names=catalog_names, rejected_names=unmatched_in_batch,
                                        failed_articles=failed_articles)
            # Phase 1 already proposes both direct and indirect company candidates.
            # Only resolve their SEC identities here; Phase 2 evaluates relevance.
            resolved=resolve_extracted_companies(conn,extracted)
            for news_id,pairs in extracted.items():
                log_step('news.correlation','ai_extracted',news_id=news_id,proposals=[{'company_name':name,'reason':reason} for name,reason in pairs])
            for news_id,pairs in resolved.items():
                log_step('news.correlation','identity_matched',news_id=news_id,matches=[{'company_id':cid,'cik':cik,'company_name':name,'reason':reason} for cid,cik,name,reason in pairs])
            ai_news_with_companies=sum(bool(pairs) for pairs in extracted.values())
            extracted_companies=sum(map(len,extracted.values()))
            resolved_companies=sum(map(len,resolved.values()))
            # Isolate extraction errors per article. An incomplete or corrupted
            # decision MUST NOT become NONE or discard other valid decisions.
            # A duplicate of a failed *new* main stays created as well, so it
            # can be reconsidered when its main is retried.
            ready_duplicates = {nid: parent for nid, parent in duplicates.items()
                                if parent not in failed_articles}
            ready_ids = set(extracted) | set(ready_duplicates)
            ready_pending = [row for row in pending if row['news_id'] in ready_ids]
            committed_relations = 0
            no_company_skipped = 0
            with conn:
                if ready_pending:
                    save_batch_groups(conn, ready_pending, list(extracted), ready_duplicates)
                for main_id, pairs in extracted.items():
                    status = save_extracted_companies(conn, main_id, pairs)
                    if status == 'skipped':
                        no_company_skipped += 1
                    else:
                        committed_relations += save_company_correlations(
                            conn, main_id, resolved[main_id])
                if extracted:
                    placeholders = ','.join('?' for _ in extracted)
                    connected = conn.execute("""SELECT COUNT(DISTINCT s.source_key) news_count,
                        COUNT(*) relation_count FROM relationships r
                        JOIN raw_items s ON s.id=r.source_id
                        WHERE r.ai_impact IS NULL AND r.entity_type='company'
                          AND s.source_type='news' AND s.status='connected'
                          AND s.source_key IN (""" + placeholders + ')', tuple(extracted)).fetchone()
                    connected_news = connected['news_count']
                    connected_relations = connected['relation_count']
                else:
                    connected_news = connected_relations = 0
            if failed_articles:
                totals['errors'] += 1
                totals['partial_batches'] += 1
                totals['failed_article_decisions'] += len(failed_articles)
                for nid, reason in failed_articles.items():
                    log_step('news.extraction', 'article_pending_retry', news_id=nid,
                             reason=reason, level='error')
                    LOG.warning('AI ARTICLE LEFT CREATED: %s %s', nid, reason)
                LOG.warning('AI BATCH [%d] partial: committed=%d article decisions, '
                            'retry_pending=%d; no fabricated NONE results',
                            batch_number, len(extracted), len(failed_articles))
            totals['relations']+=committed_relations
            totals['extracted_companies']+=extracted_companies
            totals['resolved_companies']+=resolved_companies
            totals['ai_news_with_companies']+=ai_news_with_companies
            totals['connected_news']+=connected_news
            totals['connected_relations']+=connected_relations
            totals['batches']+=int(bool(ready_pending))
            totals['main']+=len(extracted)
            totals['duplicates']+=len(ready_duplicates)
            totals['groups']+=groups
            totals['skipped']+=len(ready_duplicates)+no_company_skipped
            totals['no_company_skipped']+=no_company_skipped
            totals['catalog_unmatched']+=len(unmatched_in_batch)
            log_step('news.ai','batch_saved',batch=batch_number,main_news_ids=list(extracted),skipped_count=len(ready_duplicates)+no_company_skipped,
                     no_company_skipped=no_company_skipped,groups=groups,relations=committed_relations)
            LOG.info('AI BATCH [%d] input=%d (new=%d reference=%d) main=%d duplicate=%d groups=%d',
                     batch_number,len(pending)+len(references),len(pending),len(references),
                     len(extracted),len(ready_duplicates),groups)
            LOG.info('AI CONNECTIONS [%d] ai_news_with_companies=%d extracted_companies=%d '
                     'resolved_companies=%d connected_news=%d connected_relations=%d',
                     batch_number,ai_news_with_companies,extracted_companies,
                     resolved_companies,connected_news,connected_relations)
        except Exception as exc:
            log_step('news.ai','batch_failed',batch=batch_number,news_ids=[r['news_id'] for r in pending],reason=str(exc)[:260],level='error')
            totals['errors']+=1
            LOG.error('AI BATCH (new=%d): %s: %s; entire batch stays pending',
                      len(pending),type(exc).__name__,str(exc)[:240])
            # No batch status is committed on an invalid/failed OpenAI response.
            LOG.info('AI CONNECTIONS [%d] failed; no new connections committed',batch_number)
            # Continue with unrelated batches; next run can retry this batch.
        offset+=len(pending)
    log_step('news.ai','run_completed',**totals)
    return totals


def main(argv=None):
    p=argparse.ArgumentParser(description='Extract RSS and batch AI-group duplicate news under one main article per event')
    p.add_argument('--sources',default=config_text('NEWS_SOURCES_CSV',str(SOURCE_CSV)))
    p.add_argument('--max-feeds',type=int,default=config_int('NEWS_MAX_FEEDS',0,minimum=0),help='0=all configured sources')
    p.add_argument('--max-entries',type=int,default=config_int('NEWS_MAX_ENTRIES',0,minimum=0),help='0=all articles published within the last 3 hours')
    p.add_argument('--max-new',type=int,default=config_int('NEWS_MAX_NEW',0,minimum=0),help='0=all available new stories')
    p.add_argument('--ai-limit',type=int,default=config_int('NEWS_PHASE1_LIMIT',0,minimum=0),help='0=all pending; OpenAI usage is billed')
    p.add_argument('--batch-size',type=int,default=config_int('NEWS_PHASE1_BATCH_SIZE',500,minimum=2,maximum=500),help='2..500 total articles per AI request, including reference mains')
    p.add_argument('--retry-errors',action='store_true')
    p.add_argument('--fetch-articles',action='store_true',help='Also try downloading the primary article HTML when available')
    p.add_argument('--exposures-file',type=str,help='Optional curated company contract/exposure evidence JSON')
    p.add_argument('--ceos-file',type=str,help='Optional curated CEO identity evidence JSON')
    p.add_argument('--no-ai',action='store_true',help='Ingest only; leave pending for next AI run')
    p.add_argument('--ai-only',action='store_true',help='Process queued news without HTTP RSS fetches')
    p.add_argument('--dry-run',action='store_true',help='Count queued news; no RSS or OpenAI calls')
    p.add_argument('--save-ai-requests',action='store_true',
                   help='Save exact per-batch OpenAI instructions and input under ../data/ai_requests/ (can be large)')
    args=p.parse_args(argv);load_env()
    if not 2 <= args.batch_size <= 500:
        p.error('--batch-size must be from 2 to 500')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)-7s %(message)s')
    lock_path=database_path().parent/'news_pipeline.lock'
    with extraction_lock(lock_path) as locked:
        if not locked:
            LOG.error('Another news extractor is running; retry next scheduled hour');return 75
        with closing(connect(database_path())) as conn:
            ensure_schema(conn)
            if args.dry_run:
                now=utc_now()
                print(json.dumps({'pending_news':len(pending_news(conn,0,now=now)),
                    'pending_with_errors':len(pending_news(conn,0,True,now=now)),
                    'batch_size':args.batch_size,
                    'news_statistics':news_statistics(conn,now)},indent=2));return 0
            now=utc_now()
            stale_created_skipped=expire_pending(conn,now)
            if stale_created_skipped:
                LOG.info('News created records at least 24 hours old skipped=%d',stale_created_skipped)
            stats={'sources':0,'feed_errors':0,'read':0,'eligible':0,'inserted':0,'duplicates':0,'invalid':0,'outside_3h':0,'missing_time':0,'future':0}
            if not args.ai_only:
                sources=load_sources(args.sources)
                if args.max_feeds>0:sources=sources[:args.max_feeds]
                http=FeedClient()
                try:stats=process_news(conn,http,sources,max_entries=args.max_entries,max_new=args.max_new,now=now)
                finally:http.close()
            ai_stats={'queued':0,'batches':0,'main':0,'duplicates':0,'groups':0,'deferred':0,'errors':0}
            if not args.no_ai:
                try:
                    with openai_client() as client:
                        article_client=FeedClient(timeout=config_int('NEWS_ARTICLE_HTTP_TIMEOUT_SECONDS',8,minimum=1)) if args.fetch_articles else None
                        try:
                            # Opt-in exact request capture (no API key or model response).
                            # Each file is written immediately before its API request,
                            # including requests that subsequently time out.
                            request_recorder=None
                            if args.save_ai_requests:
                                output_dir=database_path().parent/'ai_requests'
                                output_dir.mkdir(parents=True,exist_ok=True)
                                def request_recorder(number,stage,request):
                                    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                                    filename=f'news_{stamp}_batch{number:03d}_{stage}_{uuid.uuid4().hex[:8]}.json'
                                    path=output_dir/filename
                                    path.write_text(json.dumps(request,ensure_ascii=False,indent=2),
                                                    encoding='utf-8')
                                    LOG.info('AI REQUEST [%d] %s saved=%s',number,stage,path)
                            ai_stats=process_ai(conn,client,limit=args.ai_limit,
                                retry_errors=args.retry_errors,article_client=article_client,
                                now=utc_now(),batch_size=args.batch_size,
                                exposures=load_exposures(args.exposures_file),
                                ceos=load_ceos(args.ceos_file),
                                request_recorder=request_recorder,preexpired=stale_created_skipped)
                        finally:
                            if article_client:article_client.close()
                except Exception as exc:
                    LOG.error('AI unavailable: %s: %s (unprocessed news remains queued)',type(exc).__name__,str(exc)[:260])
                    stats['ai_setup_error']=str(exc)[:200]
            ai_stats.setdefault('stale_skipped',stale_created_skipped)
            print(json.dumps({'collection':stats,'ai':ai_stats,'news_statistics':news_statistics(conn)},indent=2))
            return int(stats['feed_errors']>0 or ai_stats['errors']>0 or 'ai_setup_error' in stats)

if __name__=='__main__':raise SystemExit(main())
