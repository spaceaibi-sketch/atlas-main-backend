"""Bulk AI semantic-deduplication only; NO per-article AI scoring calls.

Up to 500 total input articles per Responses request. Model returns only
batch-local integer IDs in groups, with the main article ID first.
"""
from __future__ import annotations
from shared.processing_log import log_step
from ai.client import configured_model
from shared.config import integer as config_int
from .phase1_request import create_phase1_response
from .response_recovery import recover_json_object

import json
import logging
from pathlib import Path

LOG = logging.getLogger(__name__)
MAX_BATCH_SIZE = 500
MAX_BATCH_BYTES = config_int('NEWS_DEDUP_REQUEST_MAX_BYTES',700_000,minimum=10000,maximum=5000000)
SUMMARY_CHARS = 850
TITLE_CHARS = 300
REFERENCE_LIMIT = 70

SCHEMA = {
    'type': 'object',
    'properties': {
        'groups': {
            'type': 'array',
            'items': {'type': 'array', 'items': {'type': 'integer'}},
        },
    },
    'required': ['groups'],
    'additionalProperties': False,
}
INSTRUCTIONS = Path(__file__).with_name('BATCH_DEDUP_PROMPT.md').read_text(encoding='utf-8')


def compact_item(row, number: int, *, reference: bool) -> dict:
    return {
        'id': number,
        'kind': 'existing_main' if reference else 'new',
        'title': (row.get('title') or '')[:TITLE_CHARS],
        'summary': (row.get('article_text') or row.get('summary') or '')[:SUMMARY_CHARS],
        'source': (row.get('source') or '')[:100],
        'published_utc': row.get('published_utc') or '',
    }


def prepare_batch(news: list[dict], references: list[dict] | None = None):
    """Return compact payload + verified short-ID mapping. Never exceed 500."""
    references = references or []
    if len(news) + len(references) > MAX_BATCH_SIZE:
        raise ValueError('News AI batch exceeds 500 articles')
    all_rows = [*references, *news]
    ids = [r['news_id'] for r in all_rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Same article was submitted twice')
    data = {
        'news': [compact_item(row, i, reference=i <= len(references))
                 for i, row in enumerate(all_rows, 1)]
    }
    def encoded():
        return json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    raw = encoded()
    # Adaptive compaction keeps a full 500-ID batch within a bounded payload,
    # including multilingual UTF-8 headlines that occupy multiple bytes.
    if len(raw.encode('utf-8')) > MAX_BATCH_BYTES:
        for title_chars, summary_chars in ((270, 600), (250, 400), (220, 240),
                                           (180, 100), (120, 0)):
            for item in data['news']:
                item['title'] = item['title'][:title_chars]
                item['summary'] = item['summary'][:summary_chars]
            raw = encoded()
            if len(raw.encode('utf-8')) <= MAX_BATCH_BYTES:
                break
    if len(raw.encode('utf-8')) > MAX_BATCH_BYTES:
        raise ValueError('Even compacted news batch is too large; reduce --batch-size')
    by_index = {i: row['news_id'] for i, row in enumerate(all_rows, 1)}
    reference_ids = set(range(1, len(references) + 1))
    new_ids = set(range(len(references) + 1, len(all_rows) + 1))
    return raw, by_index, reference_ids, new_ids


def validate_groups(output, by_index, reference_ids, new_ids, failed_articles=None):
    """Reject malformed/overlapping groups atomically; never attach false IDs."""
    if not isinstance(output, dict) or set(output) != {'groups'} or not isinstance(output['groups'], list):
        raise ValueError('Batch AI must return only an array of duplicate groups')
    if failed_articles is not None:
        # Duplicate groups are independent except for overlapping membership.
        # Quarantine only new articles involved in an invalid/conflicting group;
        # unaffected groups and articles can continue to company extraction.
        memberships = {}
        bad_indices = set()
        parsed = []
        for index, supplied in enumerate(output['groups']):
            if not isinstance(supplied, list):
                LOG.warning('Ignoring unassignable non-list dedup group %s',index)
                continue
            group = [int(n) if isinstance(n, str) and n.isascii() and n.isdecimal() else n
                     for n in supplied]
            known = [n for n in group if type(n) is int and n in by_index]
            for n in set(known):
                memberships.setdefault(n, set()).add(index)
            valid = (len(group) >= 2 and len(known) == len(group) and
                     len(set(group)) == len(group) and
                     all(n in new_ids for n in group[1:]))
            if not valid and len(group) != 1:
                bad_indices.add(index)
            # Singleton output has no duplicate effect, regardless of its ID.
            parsed.append((index, group, known, valid))
        for uses in memberships.values():
            if len(uses) > 1:
                bad_indices.update(uses)
        blocked = {n for i, _group, known, _ok in parsed if i in bad_indices
                   for n in known if n in new_ids}
        for n in blocked:
            failed_articles[by_index[n]] = 'Conflicting or malformed AI duplicate group'
        duplicates = {}
        groups_count = 0
        for index, group, _known, valid in parsed:
            if index in bad_indices or not valid:
                continue
            groups_count += 1
            for duplicate in group[1:]:
                duplicates[by_index[duplicate]] = by_index[group[0]]
        mains = [by_index[n] for n in sorted(new_ids)
                 if n not in blocked and by_index[n] not in duplicates]
        return mains, duplicates, groups_count
    seen = set()
    duplicates = {}
    groups_count = 0
    for supplied in output['groups']:
        if not isinstance(supplied, list):
            raise ValueError('Each duplicate group must be an array')
        # JSON-serialized integer IDs are safe to normalize, but arbitrary text is not.
        group = [int(n) if isinstance(n, str) and n.isascii() and n.isdecimal() else n for n in supplied]
        if len(group) == 1 and type(group[0]) is int and group[0] in by_index:
            continue  # A single article has no duplicate; do not invent one.
        if len(group) < 2:
            raise ValueError('Each duplicate group requires a main and at least one duplicate')
        if any(type(n) is not int or n not in by_index for n in group):
            raise ValueError('AI returned unknown or invalid batch-local article ID')
        if len(set(group)) != len(group) or any(n in seen for n in group):
            raise ValueError('AI returned repeated/conflicting duplicate group membership')
        main = group[0]
        if not all(n in new_ids for n in group[1:]):
            raise ValueError('An existing main article cannot be demoted to a duplicate')
        seen.update(group)
        groups_count += 1
        for duplicate in group[1:]:
            duplicates[by_index[duplicate]] = by_index[main]
    mains = [by_index[n] for n in sorted(new_ids) if by_index[n] not in duplicates]
    return mains, duplicates, groups_count


def analyze_batch(client, rows, references=None, model=None, request_recorder=None, failed_articles=None):
    """ONE Responses API call for a whole article batch, never one per story."""
    payload, index, reference_ids, new_ids = prepare_batch(rows, references)
    if len(index) < 2:
        raise ValueError('At least two articles are needed for batch-only AI review')
    request = {
        'model': configured_model(model, news=True),
        'instructions': INSTRUCTIONS,
        'input': payload,
        'text': {'format': {'type': 'json_schema', 'name': 'news_event_groups_v1',
                            'schema': SCHEMA, 'strict': True}},
        'store': False,
    }
    if request_recorder is not None:
        request_recorder('event_grouping', request)
    log_step('news.dedup','request',model=request['model'],news_ids=[r['news_id'] for r in rows],reference_news_ids=[r['news_id'] for r in (references or [])])
    response = create_phase1_response(client, request)
    if getattr(response, 'status', 'completed') != 'completed' or not getattr(response, 'output_text', ''):
        raise ValueError('News batch AI response was incomplete or empty')
    result = validate_groups(recover_json_object(response.output_text), index, reference_ids, new_ids,
                             failed_articles=failed_articles)
    log_step('news.dedup','decision',main_news_ids=result[0],skipped_count=len(result[1]),group_count=result[2])
    return result
