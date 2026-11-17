"""Regression: a malformed SEC accession never stops other filings."""
from __future__ import annotations

from sec_edgar.database import add_form, connect, store_document
from sec_edgar.forms_cli import process_forms
from sec_edgar.parsing import Filing
from sec_edgar.reports import report_dir

BAD_ACC = '0001179929-26-000045'
GOOD_ACC = '0001193125-26-424052'


def _new_filing(acc: str, form: str, cik: str, stamp: str) -> Filing:
    index = (f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/'
             f'{acc.replace("-", "")}/{acc}-index.htm')
    return Filing(acc, form, cik, 'Test Issuer', stamp[:10], stamp,
                  'atom_updated', index, '', ('8.01',) if form == '8-K' else (), 'atom')


def test_exact_failing_accession_is_isolated_and_does_not_starve_new_filings(tmp_path):
    path = tmp_path / 'edgar.sqlite3'
    documents = report_dir(path)
    documents.mkdir(parents=True)
    broken = b'<html><body>Quarterly report with no Item sections</body></html>'
    valid = (b'<html><body><h1>8-K</h1><h2>Item 8.01 Other Events</h2>'
             b'<p>Issuer announced a new business agreement.</p>'
             b'<h2>Item 9.01 Exhibits</h2></body></html>')

    class OfflineClient:
        request_count = 0
        def get(self, _url):
            raise AssertionError('Reports are already cached; no network is necessary')

    with connect(path) as db:
        for acc, form, cik, stamp, body in (
            (BAD_ACC, '10-Q', '0001179929', '2026-11-02T12:00:00Z', broken),
            (GOOD_ACC, '8-K', '0000900075', '2026-11-01T12:00:00Z', valid),
        ):
            filing = _new_filing(acc, form, cik, stamp)
            assert add_form(db, filing) == 'inserted'
            form_id = db.execute('SELECT id FROM raw_items WHERE source_key=?',
                                 (filing.index_url,)).fetchone()[0]
            name = f'{acc}.htm'
            (documents / name).write_bytes(body)
            store_document(db, form_id, url=filing.index_url.rsplit('/', 1)[0] + '/primary.htm',
                           guid=f'guid-{acc}', filename=name)

        result = process_forms(db, OfflineClient(), path, limit=2)
        assert result['selected'] == 2
        assert result['errors'] == 1
        assert result['parsed'] == 1
        assert result['remaining'] == 1
        assert result['batch_status'] == 'completed_with_item_errors'
        assert result['stopped_for_sec_access'] is False
        broken_row = db.execute('SELECT status,error FROM atlas_filings WHERE index_url LIKE ?',
                                (f'%{BAD_ACC}%',)).fetchone()
        assert broken_row['status'] == 'extracted'
        assert 'No Item sections found' in broken_row['error']
        good_row = db.execute('SELECT id,status FROM atlas_filings WHERE index_url LIKE ?',
                              (f'%{GOOD_ACC}%',)).fetchone()
        assert good_row['status'] == 'parsed'
        assert db.execute('SELECT COUNT(*) FROM atlas_items WHERE form_id=?',
                          (good_row['id'],)).fetchone()[0] >= 1

        # A newly arrived filing must sort ahead of the previously errored one,
        # even with the batch limit set to 1.
        newer = _new_filing('0001193125-26-424053', '8-K', '0000900075',
                            '2026-11-02T13:00:00Z')
        assert add_form(db, newer) == 'inserted'
        new_id = db.execute('SELECT id FROM raw_items WHERE source_key=?',
                            (newer.index_url,)).fetchone()[0]
        (documents / 'new.htm').write_bytes(valid)
        store_document(db, new_id, url=newer.index_url.rsplit('/', 1)[0] + '/primary.htm',
                       guid='guid-new', filename='new.htm')
        second = process_forms(db, OfflineClient(), path, limit=1)
        assert second['selected'] == second['parsed'] == 1
        assert second['errors'] == 0
        assert second['remaining'] == 1  # old error remains visible and retryable
