"""News/SEC created_at expiration and manual force-skip; offline five-table DB."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from news_pipeline.cli import process_ai
from sec_edgar.database import connect
from sec_edgar.forms_cli import process_forms
from shared.canonical_schema import REQUIRED, table_names
from shared.stale_created import skip_created
from scripts.skip_news_filings import main as forced_cli

NOW = datetime(2026, 10, 31, 20, 0, 0, tzinfo=timezone.utc)


def _add(db, source, key, age=None, status='created', stage='created', created=None):
    ts = created if created is not None else ((NOW - timedelta(hours=age)).isoformat() if age is not None else None)
    db.execute('''INSERT INTO raw_items
      (source_type,source_key,title,source_url,status,metadata_json,created_at,updated_at)
      VALUES (?,?,?, ?, ?, ?, ?, ?)''',
      (source,key,'Sample',f'https://example.test/{key}',status,
       json.dumps({'stage':stage,'extra':'keep','form_type':'8-K'}),ts,NOW.isoformat()))
    db.commit()


def _rows(db):
    return {r['source_key']:r for r in db.execute('SELECT * FROM raw_items')}


def test_age_based_skip_uses_created_at_and_includes_exact_24_hours(tmp_path):
    with connect(tmp_path/'atlas.db') as db:
        _add(db,'news','older',age=24+1/3600)
        _add(db,'news','exact',age=24)
        _add(db,'news','new',age=1)
        _add(db,'filing','old_filing',age=30,stage='error')
        _add(db,'filing','processed',age=30,status='connected',stage='parsed')
        _add(db,'news','analysed',age=30,status='analysed')
        _add(db,'item','old_item',age=30,stage='processed')
        _add(db,'item','old_item_error',age=40,stage='error')
        _add(db,'item','exact_item',age=24,stage='processed')
        _add(db,'item','already_skipped',age=40,status='skipped')
        _add(db,'news','missing_date')
        _add(db,'filing','invalid_date',created='not-a-date')
        _add(db,'news','old_published_but_new_creation',age=2)
        db.execute("UPDATE raw_items SET event_at='2021-01-01T00:00:00Z' WHERE source_key='old_published_but_new_creation'")
        db.commit()
        assert skip_created(db,now=NOW)=={'news':2,'filing':1,'item':3}
        records=_rows(db)
        for key in ('older','exact','exact_item','old_filing','old_item','old_item_error'):
            assert records[key]['status']=='skipped'
            assert json.loads(records[key]['metadata_json'])['stage']=='skipped'
        assert json.loads(records['older']['metadata_json'])['extra']=='keep'
        assert {records[k]['status'] for k in ('new','missing_date','invalid_date','old_published_but_new_creation')}=={'created'}
        assert records['processed']['status']=='connected'
        assert records['analysed']['status']=='analysed'
        assert records['already_skipped']['status']=='skipped'
        assert records['old_filing']['created_at']==(NOW-timedelta(hours=30)).isoformat()
        assert skip_created(db,now=NOW)=={'news':0,'filing':0,'item':0}
        assert table_names(db)==REQUIRED


def test_offset_timestamps_are_checked_in_utc(tmp_path):
    with connect(tmp_path/'atlas.db') as db:
        _add(db,'news','offset',created='2026-10-30T10:00:00+03:00')
        assert skip_created(db,now=NOW)=={'news':1,'filing':0,'item':0}


def test_forced_mode_skips_all_created_regardless_age_and_is_idempotent(tmp_path):
    with connect(tmp_path/'atlas.db') as db:
        _add(db,'news','just_created',age=0)
        _add(db,'filing','missing_created_at')
        _add(db,'filing','already_connected',age=20,status='connected',stage='parsed')
        _add(db,'item','pending_item',age=20)
        assert skip_created(db,force=True,dry_run=True)=={'news':1,'filing':1,'item':1}
        assert _rows(db)['just_created']['status']=='created'
        assert skip_created(db,force=True)=={'news':1,'filing':1,'item':1}
        assert skip_created(db,force=True)=={'news':0,'filing':0,'item':0}
        rows=_rows(db)
        assert rows['just_created']['status']=='skipped'
        assert rows['missing_created_at']['status']=='skipped'
        assert rows['already_connected']['status']=='connected'
        assert rows['pending_item']['status']=='skipped'


def test_forced_cli_dry_run_and_apply(tmp_path, capsys):
    path=tmp_path/'atlas.db'
    with connect(path) as db:
        _add(db,'news','fresh',age=0)
        _add(db,'filing','fresh_filing',age=1)
        _add(db,'item','fresh_item',age=1,stage='processed')
    assert forced_cli(['--db',str(path),'--dry-run'])==0
    assert json.loads(capsys.readouterr().out)['total']==3
    with connect(path) as db:
        assert _rows(db)['fresh']['status']=='created'
    assert forced_cli(['--db',str(path),'--source','news'])==0
    assert json.loads(capsys.readouterr().out)['created_skipped']=={'news':1}
    with connect(path) as db:
        assert _rows(db)['fresh']['status']=='skipped'
        assert _rows(db)['fresh_filing']['status']=='created'
        assert _rows(db)['fresh_item']['status']=='created'
    assert forced_cli(['--db',str(path)])==0
    assert json.loads(capsys.readouterr().out)['created_skipped']=={'news':0,'filing':1,'item':1}


def test_forced_cli_item_only_skips_unscored_created_items(tmp_path, capsys):
    path = tmp_path / 'atlas.db'
    with connect(path) as db:
        _add(db, 'item', 'pending_processed_item', age=0, stage='processed')
        _add(db, 'item', 'pending_error_item', age=0, stage='error')
        _add(db, 'item', 'already_analysed_item', age=0, status='analysed')
        _add(db, 'item', 'already_skipped_item', age=0, status='skipped')
        _add(db, 'filing', 'pending_filing', age=0)
        _add(db, 'news', 'pending_news', age=0)
        # A created Item with a valid stored AI score must not be skipped.
        source_id = db.execute("SELECT id FROM raw_items WHERE source_key='pending_processed_item'").fetchone()[0]
        db.execute("INSERT INTO relationships (source_id, entity_key, ai_impact) VALUES (?, ?, ?)",
                   (source_id, 'company:123', 0))
        db.commit()
    assert forced_cli(['--db', str(path), '--source', 'item', '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['would_skip'] == {'item': 1}
    with connect(path) as db:
        assert _rows(db)['pending_error_item']['status'] == 'created'
    assert forced_cli(['--db', str(path), '--source', 'item']) == 0
    assert json.loads(capsys.readouterr().out)['created_skipped'] == {'item': 1}
    with connect(path) as db:
        rows = _rows(db)
        assert rows['pending_processed_item']['status'] == 'created'
        assert rows['pending_error_item']['status'] == 'skipped'
        assert rows['already_analysed_item']['status'] == 'analysed'
        assert rows['already_skipped_item']['status'] == 'skipped'
        assert rows['pending_filing']['status'] == 'created'
        assert rows['pending_news']['status'] == 'created'
        assert json.loads(rows['pending_error_item']['metadata_json'])['stage'] == 'skipped'


def test_legacy_source_both_preserves_news_filing_only(tmp_path, capsys):
    path = tmp_path / 'atlas.db'
    with connect(path) as db:
        for source in ('news', 'filing', 'item'):
            _add(db, source, source + '_created', age=0)
    assert forced_cli(['--db', str(path), '--source', 'both']) == 0
    assert json.loads(capsys.readouterr().out)['created_skipped'] == {'news': 1, 'filing': 1}
    with connect(path) as db:
        assert _rows(db)['item_created']['status'] == 'created'


def test_old_created_news_is_skipped_before_paid_phase_one_batch(tmp_path):
    with connect(tmp_path/'atlas.db') as db:
        _add(db,'news','stale-a',age=25)
        _add(db,'news','stale-b',age=26)
        class NoCalls:
            def __getattr__(self,name):
                raise AssertionError('No OpenAI calls should occur for expired news')
        stats=process_ai(db,NoCalls(),now=NOW)
        assert stats['stale_skipped']==2
        assert stats['queued']==0
        assert {r['status'] for r in db.execute("SELECT status FROM raw_items WHERE source_type='news'")}=={'skipped'}


def test_skipped_filing_not_selected_by_form_parser(tmp_path):
    path=tmp_path/'atlas.db'
    with connect(path) as db:
        _add(db,'filing','unprocessed',age=25)
        assert skip_created(db,sources=('filing',),now=NOW)=={'filing':1}
        class NeverHTTP:
            request_count=0
            def get(self,*a,**kw):
                raise AssertionError('Skipped filing must never be downloaded')
        stats=process_forms(db,NeverHTTP(),path,limit=100)
        assert stats['selected']==0
        assert stats['pending_start']==0


def test_forced_cli_leaves_all_rows_unchanged_if_any_pipeline_lock_busy(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import scripts.skip_news_filings as cli
    path=tmp_path/'atlas.db'
    with connect(path) as db:
        _add(db,'news','created',age=0)
    @contextmanager
    def busy_lock(path):
        yield not str(path).endswith('news_pipeline.lock')
    monkeypatch.setattr(cli,'extraction_lock',busy_lock)
    assert cli.main(['--db',str(path)])==75
    with connect(path) as db:
        assert _rows(db)['created']['status']=='created'


def test_sec_forms_entrypoint_applies_age_rule_without_http(tmp_path, monkeypatch, capsys):
    import sec_edgar.forms_cli as forms
    path=tmp_path/'atlas.db'
    with connect(path) as db:
        _add(db,'filing','stale',created=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat())
        _add(db,'filing','fresh',created=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    # Allow parsing the fresh record without any real SEC network access.
    class Client:
        request_count=0
        def __init__(self,*args,**kwargs):
            pass
        def get(self,*a,**kwargs):
            raise ValueError('Fixture request failed')
        def close(self):
            pass
    monkeypatch.setattr(forms,'SecClient',Client)
    assert forms.main(['--db',str(path),'--limit','0'])==0
    stats=json.loads(capsys.readouterr().out)
    assert stats['stale_created_skipped']==1
    assert stats['selected']==1
    with connect(path) as db:
        assert _rows(db)['stale']['status']=='skipped'


def test_news_scoring_entrypoint_ages_created_without_api_calls(tmp_path, monkeypatch, capsys):
    import news_pipeline.scoring_cli as scoring
    path=tmp_path/'atlas.db'
    with connect(path) as db:
        db.execute("INSERT INTO raw_items (source_type,source_key,status,metadata_json,created_at) VALUES ('news','stale','created','{}','2026-01-01T00:00:00Z')")
        db.commit()
    monkeypatch.setattr(scoring,'key_present',lambda:True)
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): return None
    monkeypatch.setattr(scoring,'openai_client',lambda:Client())
    monkeypatch.setattr(scoring,'score_pending',lambda *a,**k:{'errors':0,'postprocess_errors':0})
    assert scoring.main(['--db',str(path)])==0
    assert json.loads(capsys.readouterr().out)['stale_created_skipped']==1
    with connect(path) as db:
        assert _rows(db)['stale']['status']=='skipped'


def test_sec_collection_entrypoint_skips_old_created_sources(tmp_path, capsys):
    from sec_edgar.cli import main as collector_main
    path=tmp_path/'atlas.db'
    atom=tmp_path/'empty.xml'
    atom.write_text('<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    with connect(path) as db:
        _add(db,'filing','old_source',age=36)
    assert collector_main(['--db',str(path),'--import-atom',str(atom)])==0
    stats=json.loads(capsys.readouterr().out)
    assert stats['stale_created_skipped']==1
    with connect(path) as db:
        assert _rows(db)['old_source']['status']=='skipped'


def test_sec_ai_entrypoint_expires_stale_items_before_scoring(tmp_path, monkeypatch):
    import ai.cli as cli
    path = tmp_path / 'atlas.db'
    with connect(path) as db:
        _add(db, 'item', 'old_item', created=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat(), stage='processed')
        _add(db, 'item', 'new_item', created=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(), stage='processed')
    monkeypatch.setattr(cli, 'key_present', lambda: True)
    class Client:
        def close(self): pass
    monkeypatch.setattr(cli, 'openai_client', lambda: Client())
    def fake_process(db, client, **kwargs):
        assert _rows(db)['old_item']['status'] == 'skipped'
        assert _rows(db)['new_item']['status'] == 'created'
        return {'analysed': 0, 'errors': 0}
    monkeypatch.setattr(cli, 'process_pending_ai', fake_process)
    with connect(path) as db:
        result = cli.run_ai(db, path)
        assert result['stale_created_skipped'] == 1
        assert cli.run_ai(db, path)['stale_created_skipped'] == 0


def test_sec_ai_cleanup_defers_while_form_parser_busy(tmp_path, monkeypatch):
    import ai.cli as cli
    path = tmp_path / 'atlas.db'
    with connect(path) as db:
        _add(db, 'item', 'old_item', created=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat(), stage='processed')
    monkeypatch.setattr(cli, 'key_present', lambda: True)
    class Client:
        def close(self): pass
    monkeypatch.setattr(cli, 'openai_client', lambda: Client())
    monkeypatch.setattr(cli, 'process_pending_ai', lambda *a, **kw: {'errors': 0})
    from contextlib import contextmanager
    original_lock = cli.extraction_lock
    @contextmanager
    def form_busy(lock_path):
        if str(lock_path).endswith('extract_forms_items.lock'):
            yield False
        else:
            with original_lock(lock_path) as acquired:
                yield acquired
    monkeypatch.setattr(cli, 'extraction_lock', form_busy)
    with connect(path) as db:
        result = cli.run_ai(db, path)
        assert result['stale_created_skipped'] == 0
        assert _rows(db)['old_item']['status'] == 'created'
