"""Dash API contract smoke tests with a local component stand-in (no Dash install required)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types


class Component:
    def __init__(self, kind, args, kwargs):
        self.kind=kind
        self.props=kwargs
        self.children=kwargs.get('children',args[0] if args else None)


class Components:
    def __getattr__(self,name):
        return lambda *args, **kw: Component(name,args,kw)


class FakeServer:
    def route(self,path):
        return lambda func: func


class FakeDash:
    def __init__(self,*args,**kwargs):
        self.server=FakeServer()
        self.layout=None
        self.callbacks=[]
    def callback(self,*args,**kwargs):
        def wrap(func):
            self.callbacks.append((args,kwargs,func))
            return func
        return wrap


def nodes(n):
    if isinstance(n,Component):
        yield n
        yield from nodes(n.children)
    elif isinstance(n,(list,tuple)):
        for a in n:yield from nodes(a)


def app_module(monkeypatch,tmp_path):
    from sec_edgar import settings
    monkeypatch.setattr(settings,'database_path',lambda:tmp_path/'web.sqlite3')
    fake=types.ModuleType('dash')
    fake.Dash=FakeDash
    fake.Input=lambda *a,**k:('Input',a,k)
    fake.Output=lambda *a,**k:('Output',a,k)
    fake.State=lambda *a,**k:('State',a,k)
    fake.html=Components()
    fake.dcc=Components()
    fake.no_update=object()
    monkeypatch.setitem(sys.modules,'dash',fake)
    flask=types.ModuleType('flask')
    flask.abort=lambda status: None
    flask.send_file=lambda *args,**kwargs: None
    monkeypatch.setitem(sys.modules,'flask',flask)
    path=Path(__file__).resolve().parents[1]/'app.py'
    spec=importlib.util.spec_from_file_location('atlas_web_test_only',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_navigation_and_ai_controls_persist(monkeypatch,tmp_path):
    m=app_module(monkeypatch,tmp_path)
    data=list(nodes(m.app.layout))
    ids=[x.props['id'] for x in data if 'id' in x.props]
    assert len(ids)==len(set(ids)),[x for x in ids if ids.count(x)>1]
    for p in ('overview','companies','filings','items','news','quality'):
        assert 'page-'+p in ids and 'nav-'+p in ids
    assert 'top-summary' not in ids
    assert 'quality-summary' in ids
    assert 'quality-content' in ids
    assert 'item-action-bar' in ids and 'ai-ask' in ids and 'ai-analyse' in ids
    assert len(m.app.callbacks)>=10
    assert not [n for n in data if n.kind in ('Details','Summary')]
    assert m.current_page('/news')=='news'
    assert m.current_page('/quality')=='quality'
    assert m.current_page('/bogus')=='overview'
    assert m.query_id('?item=42','item')=='42'
    assert m.query_id('?news=id%20with%20spaces','news')=='id with spaces'


def test_detailed_views_and_route_callbacks(monkeypatch,tmp_path):
    m=app_module(monkeypatch,tmp_path)
    assert m.update_companies(None,'name',1,0,'/news')==(m.no_update,m.no_update)
    assert m.update_news('', 'all',1,0,'/companies',0)==(m.no_update,m.no_update)
    assert m.update_quality(0,'/news',0)==(m.no_update,m.no_update)
    body, details=m.update_quality(0,'/quality',0)
    assert 'summary-tile' in [x.props.get('className') for x in nodes(body)]
    assert 'Item extracted' in [x.children for x in nodes(body) if isinstance(x.children,str)]
    assert details
    layout=m.navigate('/items')
    assert layout[3]=={'display':'block'}
    assert layout[len(m.PATHS)+3]=='nav-link active'
    out=m.load_record('/items','?item=garbage',0)
    assert len(out)==8
    assert all(x=='workspace-grid' for x in out[4:])
    assert m.show_item_actions('/items','?item=12')=={'display':'block'}
    assert m.show_item_actions('/news','?item=12')=={'display':'none'}


def test_public_data_queries_are_read_only_when_page_loaded(monkeypatch,tmp_path):
    m=app_module(monkeypatch,tmp_path)
    company_table,count=m.update_companies('', 'name',1,0,'/companies')
    assert '0 records' in count
    assert isinstance(company_table,Component)
    news_table,count=m.update_news('', 'all',1,0,'/news',0)
    assert '0 records' in count
    table,count=m.update_items('', 'all',1,0,'/items',0)
    assert '0 records' in count


def test_quality_page_read_only_and_renders(monkeypatch,tmp_path):
    m=app_module(monkeypatch,tmp_path)
    assert m.update_quality(0, '/items',0) == (m.no_update,m.no_update)
    summary,page=m.update_quality(0, '/quality',0)
    assert summary
    names=[x.children for x in nodes(page) if isinstance(x.children,str)]
    for value in ('Latest activity','Last Item extracted','Status by stage','SEC filings',
                  'Items','News articles',
                  'News/company relationships','Data checks'):
        assert value in names
    assert not [x for x in nodes(page) if x.kind in ('Details','Summary')]
    assert m.navigate('/quality')[list(m.PATHS).index('quality')]=={'display':'block'}
