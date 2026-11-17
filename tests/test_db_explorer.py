"""DB Explorer: schema-validated, bounded, read-only functionality."""
import sqlite3

import pytest

from dashboard.db_explorer_queries import ExplorerError, PAGE_SIZE, browse, columns, list_tables
from dashboard.db_explorer_queries import authorized


@pytest.fixture
def db():
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT, score REAL, note TEXT)')
    conn.execute('CREATE TABLE "price-bars" ("id" INTEGER PRIMARY KEY, "close" REAL)')
    conn.executemany('INSERT INTO companies VALUES (?,?,?,?)',[
        (i,f'Company {i:03}',float(i)/10,None if i%7==0 else f'text_{i}%')
        for i in range(1,111)
    ])
    conn.execute('INSERT INTO "price-bars" VALUES (1,123.40)')
    conn.commit()
    try: yield conn
    finally: conn.close()


def test_tables_and_columns_are_introspected(db):
    assert list_tables(db)==['companies','price-bars']
    meta=columns(db,'companies')
    assert meta[0]['name']=='id' and meta[0]['primary_key']==1
    assert meta[1]['name']=='name'
    assert columns(db,'price-bars')[1]['name']=='close'


def test_pagination_sort_and_non_numeric_page_inputs(db):
    first=browse(db,'companies',order='score',direction='desc')
    assert len(first.rows)==PAGE_SIZE and first.has_next
    assert first.rows[0]['id']=='110'
    second=browse(db,'companies',order='id',direction='asc',page=2)
    assert second.rows[0]['id']=='41' and second.rows[-1]['id']=='80'
    tail=browse(db,'companies',order='id',direction='asc',page=3)
    assert len(tail.rows)==30 and not tail.has_next
    assert browse(db,'companies',page='oops').page==1


def test_exact_substring_null_numeric_and_quotes(db):
    result=browse(db,'companies',field='name',operator='contains',value='Company 001')
    assert [r['id'] for r in result.rows]==['1']
    assert len(browse(db,'companies',field='note',operator='null').rows)>0
    assert browse(db,'companies',field='score',operator='greater',value='10').rows[0]['id']=='110'
    assert browse(db,'companies',field='note',operator='contains',value='\\').rows==[]
    assert browse(db,'companies',field='note',operator='contains',value='%').rows
    assert browse(db,'price-bars',order='close').rows[0]['close']=='123.4'


def test_sql_injection_and_bad_filters_rejected(db):
    with pytest.raises(ExplorerError): browse(db,'companies; DROP TABLE companies;--')
    with pytest.raises(ExplorerError): browse(db,'companies',field='id) OR 1=1 --',value='1')
    with pytest.raises(ExplorerError): browse(db,'companies',direction='desc;drop table companies')
    with pytest.raises(ExplorerError): browse(db,'companies',field='score',operator='greater',value='abc')
    assert db.execute('SELECT COUNT(*) FROM companies').fetchone()[0]==110


def test_auth_is_denied_by_default_and_only_accepts_exact_secret(monkeypatch):
    monkeypatch.delenv('ATLAS_DB_EXPLORER_TOKEN',raising=False)
    assert not authorized(None) and not authorized('guess')
    monkeypatch.setenv('ATLAS_DB_EXPLORER_TOKEN','strong_example_secret')
    assert authorized('strong_example_secret')
    assert not authorized('strong_example_secre') and not authorized('')


def test_unified_raw_items_show_verified_links_and_preserve_source_metadata(db):
    # The five-table model supplies verified company links from relationships.
    # Legacy news tables are no longer a separate UI data source.
    db.execute('CREATE TABLE raw_items (id INTEGER PRIMARY KEY, source_type TEXT, title TEXT, metadata_json TEXT)')
    db.execute('CREATE TABLE relationships (source_id INTEGER, company_id INTEGER, entity_type TEXT)')
    db.execute("INSERT INTO raw_items VALUES (7,'news','Article',?)", ('{"ai_candidates":"AI Candidate Ltd.|Company 001"}',))
    db.execute("INSERT INTO relationships VALUES (7,1,'company')")
    result = browse(db,'raw_items',order='id')
    assert 'Linked companies' in result.columns
    assert 'AI Candidate Ltd.' in result.rows[0]['metadata_json']
    assert result.rows[0]['Linked companies']=='Company 001 (ID 1)'
    assert len(result.rows)==1
