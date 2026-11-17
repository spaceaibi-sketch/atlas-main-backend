"""End-to-end canonical-only smoke tests (SQLite, no network, no AI credentials)."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from shared.canonical_schema import REQUIRED, table_names
from sec_edgar.database import connect, add_form, store_document, store_items
from sec_edgar.settings import WATCH_8K_ITEMS
from news_pipeline.store import add_story, save_extracted_companies, save_company_correlations
from news_pipeline.scoring import pending_relations, _save_valid_scores
from dashboard.company_queries import company_rows
from dashboard.mobile_summary import refresh_company_summaries
from dashboard.compact_summary import summary_counts
from dashboard.explorer_queries import filings, items, news
from sec_edgar.forms_cli import process_forms


def _fixture(path):
    db=connect(path)
    item_code=sorted(WATCH_8K_ITEMS)[0]
    filing=SimpleNamespace(form_type='8-K',items=[item_code],cik='0000123456',
       company_name='Five Table Inc',index_url='https://sec.gov/Archives/edgar/test-index',
       filing_ts_utc='2026-10-01T12:00:00Z')
    assert add_form(db,filing)=='inserted'
    form_id=db.execute("SELECT id FROM raw_items WHERE source_type='filing'").fetchone()[0]
    store_document(db,form_id,url='https://sec.gov/report.htm',guid='form-guid',filename='form.htm')
    parsed=SimpleNamespace(item_code=item_code,part='',item_name='Business Event',item_text='Evidence of revenue growth')
    assert store_items(db,form_id,'form-guid',[parsed])==1
    story=SimpleNamespace(url='https://example.org/news/1',title='Five Table Inc growth',
        summary='Revenue growth confirmed.',source='Example News',region='US',category='Business',
        published_utc='2026-10-01T14:00:00Z')
    state,news_id=add_story(db,story)
    assert state=='inserted'
    assert save_extracted_companies(db,news_id,[('Five Table Inc','named')])=='extracted'
    assert save_company_correlations(db,news_id,[(1,'0000123456','Five Table Inc','direct mention')])==1
    return db,form_id,news_id


def test_ingestion_news_scoring_and_dash_use_exactly_five_tables():
    with TemporaryDirectory() as tmp:
        db,form_id,news_id=_fixture(Path(tmp)/'five.sqlite3')
        pending=pending_relations(db)
        assert len(pending)==1
        score={'relation_id':pending[0]['relation_id'],'ai_impact':2,
               'ai_confidence':8.0,'ai_reason':'The issuer reported growing revenue.',
               'ai_evidence':'Revenue growth confirmed.', 'ai_monetary_effect':0,
               'ai_monetary_effect_reason':'No amount supplied.',
               'price_change_1w_pct':0,'price_change_reason':'Cannot predict prices.'}
        _save_valid_scores(db,pending,[score])
        refresh_company_summaries(db)
        assert company_rows(db)
        assert filings(db)[1]==1
        assert items(db)[1]==1
        assert news(db)[1]==1
        assert summary_counts(db)['news_all']==1
        assert db.execute("SELECT status FROM raw_items WHERE source_key=?",(news_id,)).fetchone()[0]=='analysed'
        assert table_names(db)==REQUIRED
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone()
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        db.close()


def test_sec_form_processor_query_runs_with_only_five_tables():
    with TemporaryDirectory() as tmp:
        db,form_id,news_id=_fixture(Path(tmp)/'five.sqlite3')
        # Completed form must not be selected for download; no network calls.
        client=SimpleNamespace(get=lambda _url: (_ for _ in ()).throw(AssertionError('Unexpected network request')))
        summary=process_forms(db,client,Path(tmp)/'five.sqlite3',limit=25)
        assert summary['total_forms']==1
        assert summary['pending_start']==0
        assert table_names(db)==REQUIRED
        db.close()
