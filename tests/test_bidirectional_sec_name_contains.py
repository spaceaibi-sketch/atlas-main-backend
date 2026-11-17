"""Regression: safe two-way company-name contains and SEC jurisdiction suffixes."""
import sqlite3

from news_pipeline.matching import resolve_extracted_companies, sec_name_key
from news_pipeline.identity_validation import validate_resolved


def _db(names):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE companies (id INTEGER PRIMARY KEY, cik TEXT, name TEXT)')
    conn.executemany('INSERT INTO companies(id,cik,name) VALUES (?,?,?)',
                     [(i, f'{i:010}', name) for i,name in enumerate(names, 1)])
    return conn


def _match(conn, name):
    return resolve_extracted_companies(conn, {'news1': [(name, 'Supported event reason') ]})['news1']


def test_sec_jurisdiction_suffix_is_ignored_for_exact_and_evidence():
    conn = _db(['DEVON ENERGY CORP/DE', 'Rivian Automotive, Inc. / DE'])
    assert _match(conn, 'DEVON ENERGY CORP')[0][2] == 'DEVON ENERGY CORP/DE'
    assert _match(conn, 'Rivian Automotive, Inc.')[0][2] == 'Rivian Automotive, Inc. / DE'
    rows = [{'news_id':'news1','title':'Rivian Automotive announces new operations',
             'summary':'Rivian Automotive confirmed the launch.', 'article_text':''}]
    resolved = resolve_extracted_companies(conn, {'news1': [('Rivian Automotive, Inc.', 'Directly involved') ]})
    assert len(validate_resolved(rows, resolved)['news1']) == 1
    assert sec_name_key('Rivian Automotive, Inc. / DE') == sec_name_key('Rivian Automotive, Inc.')


def test_contains_both_directions_with_word_boundaries():
    conn = _db(['Rivian Automotive, Inc. / DE', 'Example Motors Corporation', 'Acme Holdings Inc.'])
    # SEC contains extracted company name.
    assert _match(conn, 'Rivian Automotive')[0][2] == 'Rivian Automotive, Inc. / DE'
    # AI-proposed extended name contains the shorter SEC company name.
    assert _match(conn, 'The Acme Holdings Inc. Group')[0][2] == 'Acme Holdings Inc.'
    # Do not match fragments inside a different word.
    assert _match(conn, 'Automotivee') == []


def test_ambiguous_partial_match_does_not_select_an_identity():
    conn = _db(['Rivian Automotive, Inc. / DE', 'Rivian Automotive Holdings LLC'])
    assert _match(conn, 'Rivian Automotive') == []
    # But an exact match is valid even if additional companies share the prefix.
    assert _match(conn, 'Rivian Automotive Holdings LLC')[0][2] == 'Rivian Automotive Holdings LLC'


def test_common_words_are_not_company_names():
    conn = _db(['NEWS CORP', 'PAID INC', 'POWER INC', 'One Energy Holdings'])
    for name in ('news', 'paid', 'power', 'energy', 'one'):
        assert _match(conn, name) == []


def test_does_not_mutate_extracted_company_name():
    conn = _db(['DEVON ENERGY CORP/DE'])
    proposed = [('DEVON ENERGY CORP', 'Directly named in article')]
    resolved = resolve_extracted_companies(conn, {'news1': proposed})
    assert resolved['news1'][0][2] == 'DEVON ENERGY CORP/DE'
    assert proposed == [('DEVON ENERGY CORP', 'Directly named in article')]


def test_short_article_name_resolves_unique_sec_identity_without_ceo():
    conn = _db(['AMAZON COM INC', 'Apple Inc.', 'DEVON ENERGY CORP/DE'])
    result = _match(conn, 'Amazon')
    assert len(result) == 1 and result[0][2] == 'AMAZON COM INC'
    assert _match(conn, 'DEVON ENERGY CORP')[0][2] == 'DEVON ENERGY CORP/DE'
