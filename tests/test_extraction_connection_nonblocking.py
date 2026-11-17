"""Regression: extraction may connect while Dash keeps a reader open."""
import sqlite3
from pathlib import Path
from sec_edgar.database import connect


def test_connection_does_not_change_journal_mode_or_wait_on_existing_reader(tmp_path):
    path = tmp_path / 'atlas.sqlite3'
    with connect(path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        db.execute("INSERT INTO companies(cik,name,created_at,updated_at) VALUES('0000000001','Co','t','t')")
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute('BEGIN')
    assert reader.execute('SELECT COUNT(*) FROM companies').fetchone()[0] == 1
    try:
        db = connect(path)
        try:
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
            assert db.execute('SELECT COUNT(*) FROM companies').fetchone()[0] == 1
        finally:
            db.close()
    finally:
        reader.rollback()
        reader.close()
    assert not list(tmp_path.glob('*.before_company_analytics*'))


def test_missing_company_columns_does_not_attempt_automatic_database_backup(tmp_path):
    from shared.canonical_schema import ensure_five_schema
    path=tmp_path/'atlas.sqlite3'
    with connect(path) as db:
        db.execute('ALTER TABLE companies RENAME TO old_companies')
        # End any active transaction. This is only a test DB.
        db.execute('ALTER TABLE old_companies RENAME TO companies')
    # Drop approved cache fields by explicitly editing only this synthetic test DB.
    raw = sqlite3.connect(path)
    for field in ('acumm_impact', 'acumm_monetary', 'accum_confdent'):
        raw.execute(f'ALTER TABLE companies DROP COLUMN {field}')
    raw.close()
    import pytest
    with pytest.raises(RuntimeError, match='offline schema migration'):
        connect(path)
    assert not list(tmp_path.glob('*.bak'))
