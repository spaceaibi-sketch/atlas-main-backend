"""Phase 1 wait policy: 300 seconds for each call, no automatic retries."""
import json
from types import SimpleNamespace

import pytest

from news_pipeline.batch_filter import analyze_batch as deduplicate
from news_pipeline.company_extraction import extract_companies
from news_pipeline.phase1_request import PHASE1_AI_TIMEOUT_SECONDS


ROWS = [
    {'news_id': 'n1', 'title': 'Example event 1', 'summary': 'Story one'},
    {'news_id': 'n2', 'title': 'Example event 2', 'summary': 'Story two'},
]


class ScopedClient:
    """Fake OpenAI client preserving the exact per-call SDK options."""
    def __init__(self):
        self.options = []
        self.requests = []
        self.responses = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    def create(self, **request):
        self.requests.append(request)
        if request['text']['format']['type'] == 'json_schema':
            return SimpleNamespace(status='completed', output_text=json.dumps({'groups': []}))
        return SimpleNamespace(status='completed', output_text='1|NONE\n2|NONE')


def test_both_phase1_calls_wait_up_to_five_minutes_no_retries():
    client = ScopedClient()
    mains, duplicates, groups = deduplicate(client, ROWS)
    assert mains == ['n1', 'n2'] and not duplicates and groups == 0
    proposed = extract_companies(client, ROWS, company_catalog='Apple Inc.|AAPL', catalog_names={})
    assert proposed == {'n1': [], 'n2': []}
    assert client.options == [
        {'timeout': 300.0, 'max_retries': 0},
        {'timeout': 300.0, 'max_retries': 0},
    ]
    assert all('timeout' not in x for x in client.requests)
    assert PHASE1_AI_TIMEOUT_SECONDS == 300.0


def test_timeout_policy_works_with_lightweight_fake_clients():
    class Fake:
        def __init__(self):
            self.responses = self
            self.calls = []
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(status='completed', output_text=json.dumps({'groups': []}))

    client = Fake()
    deduplicate(client, ROWS)
    assert client.calls[0]['timeout'] == 300.0


def test_timeout_exception_propagates_without_hidden_retry():
    class TimeoutClient(ScopedClient):
        def create(self, **request):
            self.requests.append(request)
            raise TimeoutError('simulated response timeout')

    client = TimeoutClient()
    with pytest.raises(TimeoutError, match='simulated'):
        deduplicate(client, ROWS)
    assert len(client.requests) == 1
    assert client.options == [{'timeout': 300.0, 'max_retries': 0}]
