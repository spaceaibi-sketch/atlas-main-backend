"""Regression checks for production log failure masking and cron false positives."""
from pathlib import Path

from ai import cli as scoring
from sec_edgar import forms_cli as forms


class _Cursor:
    def __init__(self, *, rows=None, count=1):
        self.rows, self.count = rows or [], count

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (self.count,)


class _Db:
    def __init__(self, row):
        self.row = row

    def execute(self, sql, params=()):
        if sql.startswith('SELECT id, form_type'):
            return _Cursor(rows=[self.row])
        return _Cursor(count=1)


def test_original_forms_error_survives_stage_error_log(monkeypatch, tmp_path):
    name = 'sample.htm'
    (tmp_path / name).write_text('a retained report')
    row = {'id': 9, 'form_type': '8-K',
           'index_url': 'https://www.sec.gov/Archives/edgar/data/1/0000000000-26-000001-index.html',
           'primary_url': 'https://www.sec.gov/a.htm',
           'file_guid': 'saved', 'file_name': name, 'status': 'extracted'}
    monkeypatch.setattr(forms, 'report_dir', lambda _: tmp_path)
    monkeypatch.setattr(forms, 'archive_bytes', lambda *args, **kwargs: None)
    monkeypatch.setattr(forms, 'filing_subject', lambda _: 'filing-test')
    monkeypatch.setattr(forms, 'parse_document', lambda *args, **kwargs:
                        (_ for _ in ()).throw(ValueError('original parse error')))
    saved_errors = []
    monkeypatch.setattr(forms, 'set_form_error', lambda *args, **kwargs:
                        saved_errors.append((args, kwargs)))
    seen = []
    def strict_log(stage, action, *, level='info', **context):
        seen.append((stage, action, context))
    monkeypatch.setattr(forms, 'log_step', strict_log)
    client = type('Client', (), {'request_count': 0})()
    stats = forms.process_forms(_Db(row), client, tmp_path, limit=1)
    assert stats['errors'] == 1
    assert len(saved_errors) == 1
    assert 'original parse error' in saved_errors[0][0][2]
    assert any(action == 'stage_error' and ctx.get('error_stage') == 'parse Item text'
               for _, action, ctx in seen)


def test_scoring_skip_reports_transient_failure_not_success(monkeypatch, tmp_path):
    db_path = tmp_path / 'live.sqlite3'
    db_path.touch()
    monkeypatch.setattr(scoring, 'load_env', lambda: None)
    monkeypatch.setattr(scoring, 'database_path', lambda: db_path)
    monkeypatch.setattr(scoring, 'run_ai', lambda *args, **kwargs: {'skipped_concurrent': True})

    class _Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(scoring, 'connect', lambda path: _Connection())
    assert scoring.main([]) == 75


def test_scoring_success_still_returns_zero(monkeypatch, tmp_path):
    db_path = tmp_path / 'live.sqlite3'
    db_path.touch()
    monkeypatch.setattr(scoring, 'load_env', lambda: None)
    monkeypatch.setattr(scoring, 'database_path', lambda: db_path)
    monkeypatch.setattr(scoring, 'run_ai', lambda *args, **kwargs: {'errors': 0})

    class _Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(scoring, 'connect', lambda path: _Connection())
    assert scoring.main([]) == 0
