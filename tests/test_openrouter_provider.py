"""No-network regression checks for .env-selectable OpenRouter/OpenAI Responses API."""
import sys
from types import SimpleNamespace

import pytest

from ai.client import (ai_provider, api_key_setting, configured_model, key_present,
                       load_key, openai_client)
from shared.ai_errors import brief_ai_error
from shared.ai_transport import ai_timeout_seconds, create_scoring_response


@pytest.fixture
def provider_env(monkeypatch, tmp_path):
    # The application's config loader fills missing environment variables from
    # main_back_end/.env. Point it at an empty, temporary project directory so
    # deleting a setting during a test cannot reload deployment secrets or
    # timeouts from the real .env file.
    import sec_edgar.windows_launcher as launcher
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    # Explicitly control every provider setting so host/separate tests do not leak in.
    for name in ('AI_PROVIDER', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY',
                 'OPENAI_MODEL', 'NEWS_OPENAI_MODEL', 'OPENROUTER_MODEL',
                 'NEWS_OPENROUTER_MODEL', 'OPENROUTER_BASE_URL',
                 'AI_TIMEOUT_SECONDS', 'OPENAI_TIMEOUT_SECONDS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AI_PROVIDER', 'openrouter')
    return monkeypatch


def test_openrouter_isolated_key_and_models(provider_env):
    provider_env.setenv('OPENAI_API_KEY', 'sk-direct-openai')
    provider_env.setenv('OPENROUTER_API_KEY', 'sk-or-v1-example')
    provider_env.setenv('OPENROUTER_MODEL', 'anthropic/claude-sonnet-4')
    provider_env.setenv('NEWS_OPENROUTER_MODEL', 'openai/gpt-4o-mini')
    assert ai_provider() == 'openrouter'
    assert api_key_setting() == 'OPENROUTER_API_KEY'
    assert key_present() and load_key() == 'sk-or-v1-example'
    assert configured_model() == 'anthropic/claude-sonnet-4'
    assert configured_model(news=True) == 'openai/gpt-4o-mini'
    assert configured_model('custom/model', news=True) == 'custom/model'


def test_missing_openrouter_key_never_uses_openai_key(provider_env):
    provider_env.setenv('OPENAI_API_KEY', 'sk-other-provider')
    assert not key_present()
    with pytest.raises(ValueError, match='OPENROUTER_API_KEY'):
        load_key()


def test_openrouter_base_url_and_timeout(provider_env, monkeypatch):
    seen = {}
    class FakeSDK:
        def __init__(self, **kwargs):
            seen.update(kwargs)
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=FakeSDK))
    provider_env.setenv('OPENROUTER_API_KEY', 'sk-or-credential')
    provider_env.setenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
    provider_env.setenv('AI_TIMEOUT_SECONDS', '42')
    client = openai_client()
    assert isinstance(client, FakeSDK)
    assert seen == {
        'api_key': 'sk-or-credential',
        'base_url': 'https://openrouter.ai/api/v1',
        'timeout': 42.0,
        'max_retries': 0,
    }


def test_openai_legacy_defaults_and_legacy_timeout(provider_env, monkeypatch):
    seen = {}
    class FakeSDK:
        def __init__(self, **kwargs):
            seen.update(kwargs)
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=FakeSDK))
    provider_env.setenv('AI_PROVIDER', 'openai')
    provider_env.setenv('OPENAI_API_KEY', 'sk-direct')
    provider_env.setenv('OPENAI_MODEL', 'gpt-5-mini')
    provider_env.setenv('NEWS_OPENAI_MODEL', 'gpt-5-mini-news')
    provider_env.setenv('OPENAI_TIMEOUT_SECONDS', '123')
    assert configured_model() == 'gpt-5-mini'
    assert configured_model(news=True) == 'gpt-5-mini-news'
    openai_client()
    assert seen == {'api_key': 'sk-direct', 'timeout': 123.0, 'max_retries': 0}
    assert ai_timeout_seconds() == 123.0


def test_invalid_provider_and_openrouter_url_fail_closed(provider_env):
    provider_env.setenv('AI_PROVIDER', 'unknown')
    with pytest.raises(ValueError, match='AI_PROVIDER'):
        key_present()
    provider_env.setenv('AI_PROVIDER', 'openrouter')
    provider_env.setenv('OPENROUTER_API_KEY', 'sk-or-test')
    provider_env.setenv('OPENROUTER_BASE_URL', 'http://insecure.example/api/v1')
    with pytest.raises(ValueError, match='HTTPS'):
        openai_client()


def test_transport_uses_provider_neutral_timeout(provider_env):
    provider_env.setenv('AI_TIMEOUT_SECONDS', '51')
    captured = {}
    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status='completed', output_text='{}')
    response = create_scoring_response(SimpleNamespace(responses=FakeResponses()),
                                       {'model': 'example/model', 'input': 'test'})
    assert response.status == 'completed'
    assert captured == {'model': 'example/model', 'input': 'test', 'timeout': 51.0}


def test_error_messages_redact_both_provider_keys(provider_env):
    provider_env.setenv('OPENAI_API_KEY', 'sk-secret-direct')
    provider_env.setenv('OPENROUTER_API_KEY', 'sk-secret-router')
    message = brief_ai_error(RuntimeError('sk-secret-router and sk-secret-direct failed'))
    assert 'sk-secret-router' not in message
    assert 'sk-secret-direct' not in message
    assert message.count('[REDACTED]') == 2


def test_provider_selection_and_model_from_dotenv(tmp_path, provider_env, monkeypatch):
    import sec_edgar.windows_launcher as launcher
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    provider_env.delenv('AI_PROVIDER', raising=False)
    (tmp_path / '.env').write_text(
        'AI_PROVIDER=openrouter\nOPENROUTER_API_KEY=sk-file\n'
        'OPENROUTER_MODEL=openai/gpt-4o-mini\n'
        'NEWS_OPENROUTER_MODEL=\n', encoding='utf-8')
    assert ai_provider() == 'openrouter'
    assert load_key() == 'sk-file'
    assert configured_model(news=True) == 'openai/gpt-4o-mini'


def test_openrouter_model_reaches_news_phase1_and_phase2(provider_env, monkeypatch):
    from news_pipeline import batch_filter, company_extraction, scoring

    provider_env.setenv('OPENROUTER_MODEL', 'openai/gpt-5-mini')
    provider_env.setenv('NEWS_OPENROUTER_MODEL', 'openai/gpt-4o-mini')

    captured = []
    class CapturedRequest(Exception):
        pass
    def capture(_client, request):
        captured.append(request)
        raise CapturedRequest()
    monkeypatch.setattr(batch_filter, 'create_phase1_response', capture)
    monkeypatch.setattr(company_extraction, 'create_phase1_response', capture)
    monkeypatch.setattr(scoring, 'create_scoring_response', capture)
    rows = [{'news_id': 'news-1', 'title': 'Story one'},
            {'news_id': 'news-2', 'title': 'Story two'}]
    with pytest.raises(CapturedRequest):
        batch_filter.analyze_batch(None, rows)
    with pytest.raises(CapturedRequest):
        company_extraction.extract_companies(None, rows, company_catalog='Acme Inc|ACME', catalog_names={})
    with pytest.raises(CapturedRequest):
        scoring._request_scores(None, [])
    assert len(captured) == 3
    assert [r['model'] for r in captured] == ['openai/gpt-4o-mini'] * 3
    assert captured[0]['text']['format']['type'] == 'json_schema'
    assert captured[1]['text']['format']['type'] == 'text'
    assert captured[2]['text']['format']['type'] == 'json_schema'
    assert all(r['store'] is False for r in captured)


def test_openrouter_model_reaches_sec_item_requests(provider_env, monkeypatch):
    from ai import client as scoring

    provider_env.setenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini')
    captured = []
    class CapturedRequest(Exception):
        pass
    def capture(_client, request):
        captured.append(request)
        raise CapturedRequest()
    monkeypatch.setattr(scoring, 'create_scoring_response', capture)
    monkeypatch.setattr(scoring, 'item_input', lambda row: '{}')
    monkeypatch.setattr(scoring, 'batch_input', lambda rows: '{}')
    item = {'id': 7, 'form_id': 8, 'item_type': '1.01', 'item_text': 'Hello'}
    with pytest.raises(CapturedRequest):
        scoring.analyze_item(None, item)
    with pytest.raises(CapturedRequest):
        scoring.analyze_items(None, [item])
    assert len(captured) == 2
    assert [r['model'] for r in captured] == ['openai/gpt-4o-mini'] * 2
    assert all(r['text']['format']['type'] == 'json_schema' for r in captured)
    assert all(r['store'] is False for r in captured)
