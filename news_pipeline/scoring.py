"""Phase 2 AI scoring for news/SEC-company candidate relations.

A malformed score is isolated to its relation, not the entire response batch.
Only uniquely linked company candidates reach this worker. Previously committed
scores are never overwritten by invalid or missing AI decisions.
"""
from __future__ import annotations

import json
import logging

from ai.client import create_scoring_response, configured_model
from shared.ai_scoring import batch_score_schema, validate_batch_results, completed_response_text, validate_financial_score
from shared.ai_errors import brief_ai_error
from news_pipeline.response_recovery import recover_json_object
from sec_edgar.database import now_iso
from shared.ai_fields import AI_FIELDS
from shared.processing_log import log_step

LOG = logging.getLogger(__name__)
_MAX_TEXT = 6000
_FIELDS = ('relation_id',) + AI_FIELDS

SCHEMA = batch_score_schema(id_field='relation_id', id_type='string')

INSTRUCTIONS = """You are assessing a BATCH of News-to-company candidate relationships.
Every input contains a relation_id and the identified SEC company and a possible
AI Phase 1 correlation reason. The Phase 1 reason is ONLY a candidate hypothesis,
not established evidence. Determine impact and evidence for the EXACT company.
Return one score for EVERY relation_id; never alter, duplicate or invent IDs.
For indirect correlations, evaluate whether the supplied news actually supports
the proposed business exposure. When evidence is weak, use neutral impact and low
confidence rather than inventing a business connection.
- ai_impact: signed INTEGER from -10 to +10 (0 neutral).
- ai_confidence: finite NUMBER from 0 through 10.
- ai_reason: reason grounded in the supplied article and uncertainties.
- ai_evidence: an exact verbatim excerpt from headline, summary or article text,
  or 'Not stated' when no suitable direct quotation exists.
- ai_monetary_effect: finite signed USD NUMBER when supported; otherwise use 0
  as an unquantifiable placeholder, NOT proof of no economic effect.
- ai_monetary_effect_reason: explain monetary evidence or why unquantifiable.
- price_change_1w_pct: finite NUMBER representing a speculative qualitative
  one-week market reaction, NOT a measured or guaranteed return; neutral 0 is OK.
- price_change_reason: explain catalyst, assumptions and uncertainties.
All explanation fields must be nonempty. No buy/sell advice. Strict JSON only.
"""


def pending_relations(db, limit=0, retry_errors=False):
    """Select unscored canonical relationships, one per company/source pair."""
    sql = """SELECT CAST(r.id AS TEXT) relation_id,s.source_key news_id,
      r.entity_type,r.entity_name,r.entity_key,r.company_id,
      r.match_hits,r.match_evidence,r.correlation_reason,
      s.title,s.summary,s.content article_text,s.source_name source,s.event_at published_utc
      FROM relationships r JOIN raw_items s ON s.id=r.source_id
      WHERE s.source_type='news' AND s.status='connected'
        AND r.ai_impact IS NULL AND r.entity_type='company'"""
    if not retry_errors:
        sql += " AND COALESCE(json_extract(r.metadata_json,'$.errors'),'')=''"
    sql += ' ORDER BY s.event_at DESC,s.source_key,r.entity_key'
    if limit>0:
        return [dict(x) for x in db.execute(sql+' LIMIT ?',(limit,))]
    return [dict(x) for x in db.execute(sql)]


def _material(row):
    return {key: row[key] for key in ('relation_id','news_id','entity_type','entity_name',
                                    'company_id','match_hits','match_evidence','correlation_reason',
                                    'title','source','published_utc')} | {
        'summary': (row['summary'] or '')[:2000],
        'article_text': (row['article_text'] or '')[:_MAX_TEXT],
    }


def _validate_one(result, row):
    if not isinstance(result, dict) or set(result) != set(_FIELDS):
        raise ValueError('Missing or unexpected score fields')
    if result['relation_id'] != row['relation_id']:
        raise ValueError('Mismatched relation ID')
    article = ' '.join((row['title'] or '',
        (row['summary'] or '')[:2000], (row['article_text'] or '')[:_MAX_TEXT]))
    validate_financial_score(result, evidence_text=article)
    if row['entity_type'] == 'government' and result['price_change_1w_pct'] != 0:
        raise ValueError('A government has no share price')


def _validated_partial(payload, rows):
    """Return valid scores, reasons for retry, and unexpected-result count.

    Duplicate scores for the SAME relation are quarantined, not arbitrarily
    resolved to the first/last value. Missing results stay pending for retry.
    """
    expected = {row['relation_id']: row for row in rows}
    valid, failed, unexpected = validate_batch_results(
        payload, expected, id_field='relation_id', id_type=str, validate_one=_validate_one)
    return list(valid.values()), failed, unexpected


def _validate(payload, rows):
    """Strict public validator retained for callers requiring an all-valid result."""
    good, failures, unexpected = _validated_partial(payload, rows)
    if failures or unexpected:
        raise ValueError(f'Invalid News scores: {len(failures)} relation(s), {unexpected} unexpected output(s)')
    return good


def _request_scores(client, rows, model=None):
    model_name = configured_model(model, news=True)
    log_step('news.scoring','request',model=model_name,relation_ids=[r['relation_id'] for r in rows])
    request = dict(model=model_name,
        instructions=INSTRUCTIONS,
        input=json.dumps({'news_entity_pairs': [_material(row) for row in rows]}, ensure_ascii=False),
        text={'format': {'type':'json_schema','name':'news_ai_scores_v1',
                         'schema':SCHEMA,'strict':True}}, store=False)
    response = create_scoring_response(client, request)
    return recover_json_object(completed_response_text(response, label='News'))


def analyze_batch(client, rows, model=None):
    """Strict validation helper; score_pending uses partial validation instead."""
    scores = _validate(_request_scores(client, rows, model), rows)
    for score in scores:
        log_step('news.scoring','validated',**score)
    return scores


def _save_valid_scores(db, batch, scores):
    '''Commit each relation AND its parent status in its own transaction.

    One failed SQLite update cannot roll back another relation's valid score.
    Return saved scores, stale IDs, completed article count, and write errors.
    '''
    news_for_relation = {row['relation_id']: row['news_id'] for row in batch}
    saved, stale, write_errors = [], [], {}
    news_completed = 0
    for score in scores:
        rid = score['relation_id']
        stamp = now_iso()
        try:
            with db:
                cursor = db.execute("""UPDATE relationships SET
                  ai_impact=?,ai_confidence=?,ai_reason=?,ai_evidence=?,
                  ai_monetary_effect=?,ai_monetary_effect_reason=?,
                  price_change_1w_pct=?,price_change_reason=?,
                  metadata_json=json_remove(metadata_json,'$.errors'),updated_at=?
                  WHERE CAST(id AS TEXT)=? AND ai_impact IS NULL
                   AND EXISTS (SELECT 1 FROM raw_items s WHERE s.id=relationships.source_id
                    AND s.source_type='news' AND s.status='connected')""",
                    (score['ai_impact'],score['ai_confidence'],score['ai_reason'],
                     score['ai_evidence'],score['ai_monetary_effect'],
                     score['ai_monetary_effect_reason'],score['price_change_1w_pct'],
                     score['price_change_reason'],stamp,rid))
                if cursor.rowcount:
                    nid = news_for_relation[rid]
                    old = db.execute("""SELECT status FROM raw_items WHERE source_type='news'
                        AND source_key=?""",(nid,)).fetchone()
                    outstanding=db.execute("""SELECT COUNT(*) FROM relationships r
                      JOIN raw_items s ON s.id=r.source_id
                      WHERE s.source_type='news' AND s.source_key=?
                        AND r.entity_type='company' AND r.ai_impact IS NULL""",(nid,)).fetchone()[0]
                    status='connected' if outstanding else 'analysed'
                    db.execute("""UPDATE raw_items SET status=?,updated_at=?
                       WHERE source_type='news' AND source_key=? AND status='connected'""",
                       (status,stamp,nid))
            if cursor.rowcount:
                saved.append(score)
                if status=='analysed' and old and old[0]!='analysed':
                    news_completed+=1
            else:
                stale.append(rid)
        except Exception as exc:
            write_errors[rid]=brief_ai_error(exc)
            LOG.exception('NEWS relation %s local persistence failed; continuing',rid)

    return saved, stale, news_completed, write_errors


def score_pending(db, client, *, limit=100, batch_size=20, retry_errors=False, dry_run=False, model=None):
    if limit < 0 or not 2 <= batch_size <= 100:
        raise ValueError('limit must be nonnegative and batch size must be 2..100')
    all_pending = pending_relations(db, retry_errors=retry_errors)
    rows = all_pending[:limit] if limit else all_pending
    stats = {'eligible':len(all_pending),'selected':len(rows),'batches':0,
             'analysed_relations':0,'analysed_news':0,'deferred':0,
             'errors':0,'remaining':len(all_pending), 'partial_batches':0,
             'failed_relations':0,'unexpected_results':0,'stale_skipped':0,
             'postprocess_errors':0,'batch_fallbacks':0}
    if dry_run:
        log_step('news.scoring','dry_run_completed',**stats)
        return stats
    def commit_valid(batch, good):
        saved, stale, completed, write_errors = _save_valid_scores(db, batch, good)
        stats['analysed_relations'] += len(saved)
        stats['analysed_news'] += completed
        stats['stale_skipped'] += len(stale)
        stats['errors'] += len(write_errors)
        for score in saved:
            log_step('news.scoring', 'relation_saved', relation_id=score['relation_id'],
                     impact=score['ai_impact'], confidence=score['ai_confidence'])
            LOG.info('NEWS ANALYSED relation=%s impact=%s confidence=%s',
                     score['relation_id'],score['ai_impact'],score['ai_confidence'])
        for rid, reason in write_errors.items():
            log_step('news.scoring', 'relation_write_failed', relation_id=rid,
                     reason=reason, level='error')

    def pending_failure(relation_id, reason):
        LOG.warning('NEWS AI RELATION RETRY: %s: %s',relation_id,reason)
        log_step('news.scoring','relation_pending_retry',relation_id=relation_id,
                 reason=str(reason)[:220],level='error')

    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        stats['batches'] += 1
        try:
            good, failures, unknown = _validated_partial(_request_scores(client, batch, model),batch)
        except Exception as exc:
            if type(exc).__name__ in ('AuthenticationError','PermissionDeniedError','RateLimitError'):
                stats['errors'] += 1
                LOG.error('NEWS AI account or quota failure: %s',brief_ai_error(exc))
                break
            # An invalid envelope or a failed multi-relation request gives no
            # reliable attribution to individual relations. Salvage one by one,
            # and save each recovered relation BEFORE requesting the next one.
            stats['batch_fallbacks'] += 1
            LOG.warning('NEWS AI batch request unusable (%s); isolating %d relations',
                        brief_ai_error(exc), len(batch))
            failed_count = 0
            aborted = False
            for row in batch:
                rid = row['relation_id']
                try:
                    singles, problems, unexpected = _validated_partial(
                        _request_scores(client,[row],model),[row])
                except Exception as relation_error:
                    if type(relation_error).__name__ in ('AuthenticationError','PermissionDeniedError','RateLimitError'):
                        stats['errors'] += 1
                        LOG.error('NEWS AI account or quota failure: %s',brief_ai_error(relation_error))
                        aborted = True
                        break
                    failed_count += 1
                    pending_failure(rid, brief_ai_error(relation_error))
                    continue
                stats['unexpected_results'] += unexpected
                if problems or unexpected:
                    failed_count += 1
                    pending_failure(rid,problems.get(rid,'Unexpected relation score ID'))
                if singles:
                    commit_valid([row], singles)
            stats['failed_relations'] += failed_count
            if failed_count:
                stats['partial_batches'] += 1
                # One unusable request group counts as one API error;
                # failed_relations provides the exact individual count.
                stats['errors'] += 1
            LOG.info('NEWS AI batch %d fallback recovered=%d failed_relations=%d',
                     stats['batches'],len(batch)-failed_count,failed_count)
            if aborted:
                break
            continue

        stats['unexpected_results'] += unknown
        # Important: commit successful scores first. No invalid, missing or
        # duplicated relation may delay or roll back an independent success.
        commit_valid(batch, good)
        if failures or unknown:
            stats['partial_batches'] += 1
            stats['failed_relations'] += len(failures)
            stats['errors'] += len(failures) + int(bool(unknown))
            for rid, reason in failures.items():
                pending_failure(rid,reason)
            if unknown:
                LOG.warning('NEWS AI ignored %d unknown/malformed score entries',unknown)
        LOG.info('NEWS AI batch %d valid=%d retry_pending=%d',
                 stats['batches'],len(good),len(failures))
    if stats['analysed_relations']:
        from shared.aggregate_refresh import refresh_score_caches
        stats['postprocess_errors'] += refresh_score_caches(db, LOG)
    stats['remaining'] = len(pending_relations(db,retry_errors=retry_errors))
    log_step('news.scoring','run_completed',**stats)
    return stats
