"""Reusable, provider-independent scoring helpers for News and SEC Items.

This module validates the same eight financial fields in both pipelines. It
intentionally does not choose prompts, queue rows, retry policies, or write DB
statuses: those are different for SEC Items and News relations.
"""
from __future__ import annotations

import math
import unicodedata

from shared.ai_fields import AI_FIELDS

_SCORE_TYPES = {
    'ai_impact': {'type': 'integer'},
    'ai_confidence': {'type': 'number'},
    'ai_reason': {'type': 'string'},
    'ai_evidence': {'type': 'string'},
    'ai_monetary_effect': {'type': 'number'},
    'ai_monetary_effect_reason': {'type': 'string'},
    'price_change_1w_pct': {'type': 'number'},
    'price_change_reason': {'type': 'string'},
}


def financial_score_schema(*, leading_properties=None):
    """Build a fresh strict JSON schema; News prepends its relation identifier."""
    properties = {name: dict(value) for name, value in (leading_properties or {}).items()}
    properties.update({name: dict(_SCORE_TYPES[name]) for name in AI_FIELDS})
    return {
        'type': 'object',
        'properties': properties,
        'required': list(properties),
        'additionalProperties': False,
    }


def completed_response_text(response, *, label):
    """Read a completed Responses API result without guessing missing output."""
    status = getattr(response, 'status', None)
    if status not in (None, 'completed'):
        raise ValueError(f'{label} AI response incomplete: {status}')
    raw = getattr(response, 'output_text', '')
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f'{label} AI returned no structured response')
    return raw


def validate_financial_score(result, *, evidence_text):
    """Validate the shared eight fields and literal quote against supplied text.

    ``result`` may have caller-owned keys (e.g. relation_id); the caller must
    separately validate those and reject unexpected fields. Supplying the
    exact text exposed to AI prevents evidence checks against hidden text.
    """
    if not isinstance(result, dict):
        raise ValueError('AI score must be an object')
    impact = result['ai_impact']
    if type(impact) is not int or not -10 <= impact <= 10:
        raise ValueError('ai_impact must be an integer -10..10')
    confidence = result['ai_confidence']
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 10:
        raise ValueError('ai_confidence must be finite 0..10')
    for key in ('ai_monetary_effect', 'price_change_1w_pct'):
        value = result[key]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f'{key} must be a finite number')
    for key in ('ai_reason', 'ai_evidence', 'ai_monetary_effect_reason', 'price_change_reason'):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f'{key} explanation is missing')
    # Unicode compatibility (e.g., decomposed accents) and whitespace
    # changes must not create false negatives, but never accept paraphrases.
    quote = ' '.join(unicodedata.normalize('NFKC', result['ai_evidence']).split())
    text = ' '.join(unicodedata.normalize('NFKC', evidence_text).split())
    if quote.casefold() != 'not stated' and quote not in text:
        raise ValueError('Verbatim evidence is absent from supplied text')


def batch_score_schema(*, id_field, id_type):
    """Fresh strict shared results[] schema; caller chooses stable identifier."""
    if id_field not in ('relation_id', 'item_id') or id_type not in ('string', 'integer'):
        raise ValueError('Unsupported scoring result identity')
    return {
        'type': 'object',
        'properties': {'results': {'type': 'array', 'items': financial_score_schema(
            leading_properties={id_field: {'type': id_type}})}},
        'required': ['results'], 'additionalProperties': False,
    }


def validate_batch_results(payload, expected, *, id_field, id_type, validate_one):
    """Isolate missing, duplicate, invalid and unknown AI decisions by requested ID.

    ``expected`` maps stable IDs to source rows. Return (valid_by_id,
    failure_by_id, unknown_count). Unlike scoring workers this has no DB
    writes, fallback requests, prompt assumptions or status transitions.
    """
    if not isinstance(payload, dict) or set(payload) != {'results'} or not isinstance(payload['results'], list):
        raise ValueError('Scoring response must contain only a results array')
    if id_type not in (str, int):
        raise ValueError('Unsupported batch ID type')
    grouped = {}
    unexpected = 0
    for item in payload['results']:
        value = item.get(id_field) if isinstance(item, dict) else None
        if type(value) is not id_type or value not in expected:
            unexpected += 1
            continue
        grouped.setdefault(value, []).append(item)
    valid, failed = {}, {}
    for identifier, row in expected.items():
        entries = grouped.get(identifier, ())
        if len(entries) != 1:
            failed[identifier] = 'Missing AI score' if not entries else 'Duplicate AI scores for ID'
            continue
        try:
            entry = entries[0]
            validate_one(entry, row)
        except (ValueError, TypeError, KeyError) as error:
            failed[identifier] = str(error)
        else:
            valid[identifier] = entry
    return valid, failed, unexpected
