"""Provider-configured Responses API adapter (OpenAI or OpenRouter)."""
from __future__ import annotations
from shared.processing_log import log_step
import json
from pathlib import Path
from shared.config import text as setting
from .prompt import (INSTRUCTIONS, SCHEMA, FIELDS, item_input,
                     BATCH_INSTRUCTIONS, BATCH_SCHEMA, batch_input)
from shared.ai_scoring import completed_response_text, validate_financial_score, validate_batch_results
from shared.ai_transport import create_scoring_response, ai_timeout_seconds

KEY_PATH = Path(__file__).resolve().parents[1] / 'assets' / 'key.txt'
DEFAULT_MODEL = 'gpt-5-mini'

EXACT_QUOTE_RETRY_INSTRUCTIONS = """
IMPORTANT RETRY: A previous attempt quoted evidence that was not a literal
substring of the supplied item_text. Re-read ONLY this Item. The ai_evidence
value MUST contain one contiguous, exactly copied passage from item_text.
Keep every original character and punctuation mark; do not paraphrase, add
ellipses, combine fragments, or add page references. If no suitable evidence
passage exists, output exactly 'Not stated'. Still provide all eight scoring
fields with conservative, text-grounded explanations. Strict JSON only.
"""


def ai_provider() -> str:
    """Select one API from .env without silently falling back to another."""
    provider = setting('AI_PROVIDER', 'openai').strip().lower()
    if provider not in ('openai', 'openrouter'):
        raise ValueError('AI_PROVIDER must be openai or openrouter in main_back_end/.env')
    return provider


def api_key_setting() -> str:
    return 'OPENROUTER_API_KEY' if ai_provider() == 'openrouter' else 'OPENAI_API_KEY'


def _valid_key(value: str) -> bool:
    return bool(value and not value.startswith(('PASTE_', 'YOUR_', '#'))
                and '\n' not in value and '\r' not in value)


def key_present(path: Path = KEY_PATH) -> bool:
    """Whether the selected provider's real key is set; `path` is legacy."""
    return _valid_key(setting(api_key_setting()).strip())


def load_key(path: Path = KEY_PATH) -> str:
    """Read only the selected provider's key from .env, never assets/key.txt."""
    name = api_key_setting()
    value = setting(name).strip()
    if not _valid_key(value):
        raise ValueError(f'Set {name} in main_back_end/.env')
    return value


def configured_model(model: str | None = None, *, news: bool = False) -> str:
    """Select an explicit CLI model or the selected provider's .env model."""
    if model is not None:
        return model
    if ai_provider() == 'openrouter':
        base_model = setting('OPENROUTER_MODEL', 'openai/gpt-5-mini').strip()
        return (setting('NEWS_OPENROUTER_MODEL').strip() or base_model) if news else base_model
    base_model = setting('OPENAI_MODEL', DEFAULT_MODEL).strip()
    return (setting('NEWS_OPENAI_MODEL').strip() or base_model) if news else base_model


def openai_client(path: Path = KEY_PATH):
    """Existing public factory name retained for callers and test mocks.

    The OpenAI Python SDK supports both providers' Responses endpoints.
    """
    options = {'api_key': load_key(path), 'timeout': ai_timeout_seconds(),
               'max_retries': 0}
    if ai_provider() == 'openrouter':
        base_url = setting('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1').strip()
        if not base_url.startswith('https://'):
            raise ValueError('OPENROUTER_BASE_URL must be an HTTPS URL')
        options['base_url'] = base_url
    from openai import OpenAI
    return OpenAI(**options)


def analyze_item(client, row, model: str | None = None, *, exact_quote_retry=False):
    model_name = configured_model(model)
    log_step('ai.item','request',item_id=row['id'],form_id=row['form_id'],item_type=row['item_type'],model=model_name,text_chars=len(row['item_text']))
    response = create_scoring_response(client, dict(
        model=model_name,
        instructions=INSTRUCTIONS + (EXACT_QUOTE_RETRY_INSTRUCTIONS if exact_quote_retry else ''),
        input=item_input(row),
        text={'format': {'type': 'json_schema', 'name': 'sec_item_analysis',
                         'schema': SCHEMA, 'strict': True}},
        store=False,
    ))
    raw = completed_response_text(response, label='SEC Item')
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != set(FIELDS):
        raise ValueError('AI returned missing or unexpected fields')
    validate_financial_score(result, evidence_text=row['item_text'])
    log_step('ai.item','validated',item_id=row['id'],form_id=row['form_id'],impact=result['ai_impact'],confidence=result['ai_confidence'],monetary_usd=result['ai_monetary_effect'],price_change_1w_pct=result['price_change_1w_pct'],impact_reason=result['ai_reason'],evidence=result['ai_evidence'],monetary_reason=result['ai_monetary_effect_reason'],price_reason=result['price_change_reason'])
    return result


def analyze_items(client, rows, model: str | None = None):
    """Assess multiple SEC Items in one paid request; isolate bad results by ID.

    Return (valid_score_by_item_id, validation_error_by_item_id, unknown_count).
    Transport failures or completely unparsable batch envelopes still raise;
    the worker handles those without discarding results from other batches.
    """
    if not rows:
        return {}, {}, 0
    item_ids = [int(row['id']) for row in rows]
    if len(set(item_ids)) != len(item_ids):
        raise ValueError('Duplicate SEC Item IDs in request')
    model_name = configured_model(model)
    log_step('ai.item','batch_request',item_ids=item_ids,model=model_name)
    response = create_scoring_response(client, dict(
        model=model_name,
        instructions=BATCH_INSTRUCTIONS,
        input=batch_input(rows),
        text={'format': {'type': 'json_schema', 'name': 'sec_item_batch_analysis',
                         'schema': BATCH_SCHEMA, 'strict': True}},
        store=False,
    ))
    payload = json.loads(completed_response_text(response, label='SEC Item batch'))
    expected = {int(row['id']): row for row in rows}

    def validate_one(entry, row):
        if set(entry) != set(FIELDS) | {'item_id'}:
            raise ValueError('Missing or unexpected SEC scoring fields')
        validate_financial_score(entry, evidence_text=row['item_text'])

    valid, errors, unexpected = validate_batch_results(
        payload, expected, id_field='item_id', id_type=int, validate_one=validate_one)
    scores = {item_id: {field: entry[field] for field in FIELDS}
              for item_id, entry in valid.items()}
    return scores, errors, unexpected
