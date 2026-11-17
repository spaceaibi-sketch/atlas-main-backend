"""Check that the simplified UI is flat and retains the essential data."""
from __future__ import annotations

import sys
import types
from pathlib import Path

from dashboard.news_views import display_news_pair


class FakeHtml:
    def __getattr__(self, name):
        def component(*args, **kwargs):
            return {'tag': name, 'children': args[0] if args else kwargs.get('children'),
                    'props': kwargs}
        return component


def tree(node):
    if isinstance(node, dict):
        yield node
        yield from tree(node['children'])
    elif isinstance(node, (list, tuple)):
        for sub in node:
            yield from tree(sub)


def text(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        yield from text(node['children'])
    elif isinstance(node, (list, tuple)):
        for sub in node:
            yield from text(sub)


def test_news_details_show_current_match_reason_no_expandable_ui(monkeypatch):
    monkeypatch.setitem(sys.modules, 'dash', types.SimpleNamespace(html=FakeHtml()))
    news=dict(title='Saudi regulatory change', summary='Announcement details',
              article_text='', correlation_reason='Verified contract affected by rule',
              source='Test RSS', published_utc='2026-10-17T14:00:00+00:00',
              url='https://example.com/article', entity_name='Example Company',
              news_status='connected', ai_impact=None, ai_confidence=None)
    view=display_news_pair(news)
    tags=[c['tag'] for c in tree(view)]
    body=' '.join(text(view))
    assert 'Details' not in tags and 'Summary' not in tags
    assert 'Verified contract affected by rule' in body
    assert 'Article summary' in body
    assert 'Duplicate coverage' not in body and 'Legacy relationship' not in body
    assert 'AI assessment not completed' in body


def test_dashboard_source_contains_no_expandable_help_or_notices():
    source=(Path(__file__).resolve().parents[1]/'app.py').read_text()
    assert 'html.Details(' not in source
    assert 'html.Summary(' not in source
    assert 'One population. Four stages.' not in source
    assert "id='top-summary'" not in source
    assert "id='quality-summary'" in source
