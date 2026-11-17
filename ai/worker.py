"Incremental Item-to-OpenAI processor; SQLite transaction per Item."
from __future__ import annotations
from shared.processing_log import log_step
import logging
import json
from shared.ai_errors import brief_ai_error
from sec_edgar.database import now_iso
from .client import analyze_item, analyze_items
from .prompt import SCENARIO_REASON_PREFIX



LOG = logging.getLogger(__name__)


def process_pending_ai(db, client, *, limit=25, retry_errors=False, reanalyse_zero_prices=False, model=None, dry_run=False, batch_size=20):
    if limit < 0:
        raise ValueError('--limit must be >= 0')
    if type(batch_size) is not int or not 1 <= batch_size <= 25:
        raise ValueError('--batch-size must be 1..25')
    allowed = ('processed', 'error') if retry_errors else ('processed',)
    placeholders = ','.join('?' for _ in allowed)
    where = f"i.status IN ({placeholders})"
    where = f"({where}) AND f.status='parsed'"
    # Opt-in upgrade of old completed zero-price assessments; a zero estimate
    # can also be a legitimate neutral assessment, so this is NEVER automatic.
    if reanalyse_zero_prices:
        where = f"(f.status='parsed' AND (i.status IN ({placeholders}) OR (i.status='analysed' AND i.price_change_1w_pct=0 AND i.price_change_reason NOT LIKE 'Qualitative scenario (not an observed return): %')))"
    query = f'''SELECT i.*, f.form_type, c.cik, c.name company_name, f.index_url
      FROM atlas_items i JOIN atlas_filings f ON i.form_id=f.id
      JOIN companies c ON c.id=f.company_id
      WHERE {where} ORDER BY f.filed_date DESC, i.form_id, i.item_type, i.item_occurrence'''
    count = db.execute(f'SELECT COUNT(*) FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id WHERE {where}', allowed).fetchone()[0]
    rows = db.execute(query + (' LIMIT ?' if limit else ''),
                      (*allowed, limit) if limit else allowed).fetchall()
    stats = dict(eligible=count, selected=len(rows), reanalysed_zero_prices=0, analysed=0, rejected=0,
                 errors=0, skipped_stale=0, remaining=count, postprocess_errors=0,
                 batches=0, partial_batches=0, unexpected_scores=0, batch_fallbacks=0,
                 evidence_retries=0, evidence_recovered=0)
    LOG.info('AI eligible=%d selected=%d model=%s dry_run=%s',count,len(rows),model or 'configured',dry_run)
    if dry_run:
        log_step('ai.item','dry_run_completed',**stats)
        return stats
    def _save(row, result, position):
        """Persist exactly one independently validated result with in-flight guards."""
        guid = row['id']
        with db:
            source=db.execute("""SELECT id FROM raw_items
              WHERE source_type='item' AND content=?
                AND (id=? OR CAST(json_extract(metadata_json,'$.id') AS INTEGER)=?)
                AND (CASE WHEN status='analysed' THEN status
                          ELSE COALESCE(json_extract(metadata_json,'$.stage'),'processed')
                     END)=?""",(row['item_text'],guid,guid,row['status'])).fetchone()
            if source:
                cursor=db.execute("""UPDATE relationships SET
                  ai_impact=?,ai_confidence=?,ai_reason=?,ai_evidence=?,
                  ai_monetary_effect=?,ai_monetary_effect_reason=?,
                  price_change_1w_pct=?,price_change_reason=?,updated_at=?
                  WHERE source_id=? AND entity_type='company'""",
                  (result['ai_impact'], result['ai_confidence'], result['ai_reason'],
                   result['ai_evidence'], result['ai_monetary_effect'],
                   result['ai_monetary_effect_reason'],result['price_change_1w_pct'],
                   SCENARIO_REASON_PREFIX+result['price_change_reason'],now_iso(),source['id']))
                if cursor.rowcount:
                    db.execute("""UPDATE raw_items SET status='analysed',
                      metadata_json=json_set(metadata_json,'$.stage','analysed','$.errors',''),
                      updated_at=? WHERE id=?""",(now_iso(),source['id']))
            else:
                class _NoChange:
                    rowcount=0
                cursor=_NoChange()
        if not cursor.rowcount:
            stats['skipped_stale'] += 1
            log_step('ai.item','stale',item_id=guid,form_id=row['form_id'])
            return
        log_step('ai.item','saved',item_id=guid,form_id=row['form_id'],
                 item_type=row['item_type'],impact=result['ai_impact'],
                 confidence=result['ai_confidence'],monetary_usd=result['ai_monetary_effect'],
                 price_change_1w_pct=result['price_change_1w_pct'],
                 impact_reason=result['ai_reason'],evidence=result['ai_evidence'],
                 monetary_reason=result['ai_monetary_effect_reason'],
                 price_reason=result['price_change_reason'])
        stats['analysed'] += 1
        if row['status'] == 'analysed':
            stats['reanalysed_zero_prices'] += 1
        LOG.info('[%d/%d] ANALYSED %s item=%s impact=%s confidence=%.1f',
                 position, len(rows), row['index_url'], row['item_type'],
                 result['ai_impact'], result['ai_confidence'])

    def _fail(row, reason, position):
        """Only this Item fails; keep scores on already-analysed Items."""
        guid = row['id']
        message = brief_ai_error(reason) if isinstance(reason, BaseException) else str(reason)[:220]
        with db:
            cursor=db.execute("""UPDATE raw_items SET
               metadata_json=json_set(metadata_json,'$.errors',?, '$.stage',
                   CASE WHEN status='analysed' THEN 'analysed' ELSE 'error' END),
               updated_at=? WHERE source_type='item' AND content=?
               AND (id=? OR CAST(json_extract(metadata_json,'$.id') AS INTEGER)=?)
               AND (CASE WHEN status='analysed' THEN status
                   ELSE COALESCE(json_extract(metadata_json,'$.stage'),'processed') END)=?""",
               (message,now_iso(),row['item_text'],guid,guid,row['status']))
        if cursor.rowcount:
            log_step('ai.item','failed',item_id=guid,form_id=row['form_id'],reason=message,level='error')
        else:
            stats['skipped_stale'] += 1
            log_step('ai.item','stale',item_id=guid,form_id=row['form_id'],reason=message)
        stats['errors'] += 1
        LOG.error('[%d/%d] AI FAILED %s item=%s: %s',
                  position,len(rows),row['index_url'],row['item_type'],message)

    # Retry bad quotes only AFTER all other batches have been processed.
    # An individual invalid Item must not delay a later batch's good Items.
    evidence_queue = []
    account_blocked = False
    # SQLite commits are per Item even when transport is batched. A Python
    # loop is necessary: each Item has its own guarded transaction/status.
    for offset in range(0, len(rows), batch_size):
        group = rows[offset:offset + batch_size]
        active = []
        for position, row in enumerate(group, offset + 1):
            guid = row['id']
            log_step('ai.item','selected',item_id=guid,form_id=row['form_id'],
                     item_type=row['item_type'],previous_status=row['status'],
                     position=position,total=len(rows))
            if row['item_text'].strip():
                active.append((position, row))
                continue
            # Empty Item text is never sent to a paid request.
            with db:
                cursor=db.execute("""UPDATE raw_items SET
                    metadata_json=json_set(metadata_json,'$.stage',
                       CASE WHEN status='analysed' THEN 'analysed' ELSE 'rejected' END,
                       '$.errors','Empty Item text'),updated_at=?
                    WHERE source_type='item' AND content=?
                     AND (id=? OR CAST(json_extract(metadata_json,'$.id') AS INTEGER)=?)
                     AND (CASE WHEN status='analysed' THEN status
                         ELSE COALESCE(json_extract(metadata_json,'$.stage'),'processed') END)=?""",
                     (now_iso(),row['item_text'],guid,guid,row['status']))
            if not cursor.rowcount:
                stats['skipped_stale'] += 1
                log_step('ai.item','stale',item_id=guid,form_id=row['form_id'])
            else:
                log_step('ai.item','rejected',item_id=guid,reason='Empty Item text')
                if row['status'] == 'analysed':
                    stats['errors'] += 1
                else:
                    stats['rejected'] += 1
        if not active:
            continue
        current_rows = [row for _, row in active]
        stats['batches'] += 1

        def commit_one(position, row, *, score=None, failure=None):
            """One result, one transaction; a write/logging error cannot stop peers."""
            try:
                if score is not None:
                    _save(row, score, position)
                else:
                    _fail(row, failure, position)
            except Exception:
                stats['errors'] += 1
                LOG.exception('[%d/%d] SEC Item %s local persistence failed; continuing',
                              position, len(rows), row['id'])

        def queue_or_fail(position, row, reason):
            """Mark normal failures immediately, but defer paid quote repairs."""
            if 'Verbatim evidence is absent' in str(reason):
                evidence_queue.append((position, row))
                LOG.warning('SEC Item %s returned a nonverbatim quote; queued this Item for one retry after remaining batches', row['id'])
            else:
                commit_one(position, row, failure=reason)

        try:
            if len(current_rows) == 1:
                row = current_rows[0]
                scores, failures, unexpected = {row['id']: analyze_item(client,row,model=model)}, {}, 0
            else:
                scores, failures, unexpected = analyze_items(client,current_rows,model=model)
        except (ValueError, json.JSONDecodeError) as batch_error:
            if len(current_rows) == 1:
                scores, failures, unexpected = {}, {current_rows[0]['id']: batch_error}, 0
            else:
                # The response envelope cannot be assigned to specific IDs.
                # Save each singleton AS SOON AS it returns, rather than waiting
                # for every fallback/retry to finish before writing successes.
                stats['batch_fallbacks'] += 1
                LOG.warning('SEC AI batch output unusable (%s); retrying %d Items individually',
                            brief_ai_error(batch_error), len(current_rows))
                aborted = False
                for position, row in active:
                    try:
                        score = analyze_item(client, row, model=model)
                    except Exception as exc:
                        if type(exc).__name__ in ('AuthenticationError', 'PermissionDeniedError', 'RateLimitError'):
                            stats['errors'] += 1
                            LOG.error('SEC AI authentication, permission or quota failure: %s',
                                      brief_ai_error(exc))
                            aborted = True
                            account_blocked = True
                            break  # Unattempted rows remain eligible.
                        queue_or_fail(position, row, exc)
                    else:
                        commit_one(position, row, score=score)
                if aborted:
                    break
                continue
        except Exception as exc:
            if type(exc).__name__ in ('AuthenticationError','PermissionDeniedError','RateLimitError'):
                # Preserve pending rows rather than treating an account outage
                # as a document-specific error.
                stats['errors'] += 1
                LOG.error('SEC AI authentication, permission or quota failure: %s', brief_ai_error(exc))
                account_blocked = True
                break
            # Request-level failure has no Item-specific evidence. Do not mark
            # unrelated Items as failed; try subsequent batches independently.
            stats['errors'] += 1
            LOG.error('SEC AI batch request failed, %d Items remain pending: %s',
                      len(current_rows), brief_ai_error(exc))
            continue

        stats['unexpected_scores'] += unexpected
        if unexpected:
            LOG.warning('SEC AI returned %d unknown/invalid Item IDs; ignored', unexpected)
        if scores and failures:
            stats['partial_batches'] += 1

        # The initial valid scores are committed BEFORE ANY quote-repair HTTP
        # requests. A slow or failing Item cannot delay successful neighbors.
        for position, row in active:
            score = scores.get(row['id'])
            if score is not None:
                commit_one(position, row, score=score)

        # Invalid fields fail independently; only bad quotes enter the retry
        # queue. No retry request is made until ALL primary batches finish.
        for position, row in active:
            if row['id'] not in scores:
                queue_or_fail(position, row, failures.get(row['id'], 'Missing SEC AI score'))

    if not account_blocked:
        for position, row in evidence_queue:
            stats['evidence_retries'] += 1
            LOG.warning('SEC Item %s retrying its nonverbatim quote once', row['id'])
            try:
                repaired = analyze_item(client, row, model=model, exact_quote_retry=True)
            except Exception as exc:
                if type(exc).__name__ in ('AuthenticationError', 'PermissionDeniedError', 'RateLimitError'):
                    stats['errors'] += 1
                    LOG.error('SEC AI account or quota failure while retrying Item %s: %s',
                              row['id'], brief_ai_error(exc))
                    break  # Other queued retries remain pending, not failed.
                commit_one(position, row, failure=exc)
            else:
                stats['evidence_recovered'] += 1
                commit_one(position, row, score=repaired)
    if stats['analysed']:
        from shared.aggregate_refresh import refresh_score_caches
        stats['postprocess_errors'] += refresh_score_caches(db, LOG)
    stats['remaining'] = db.execute(f'SELECT COUNT(*) FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id WHERE {where}', allowed).fetchone()[0]
    log_step('ai.item','run_completed',**stats)
    return stats
