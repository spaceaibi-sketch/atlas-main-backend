"""Regression for October 17 SEC filing-detail primary-document failures."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from sec_edgar.database import add_form, connect
from sec_edgar.forms_cli import process_forms
from sec_edgar.parsing import Filing
from sec_edgar.reports import (
    complete_submission_url,
    primary_document_from_submission,
    primary_document_url,
    sec_access_problem,
)


KNOWN_FILINGS = [
    ('0001104659-26-116551', '1159408', 'tm2626710d25_8k.htm'),
    ('0001104659-26-116550', '1159408', 'tm2626710d24_8k.htm'),
    ('0001193125-26-423749', '2150706', 'd285775d8k.htm'),
]
REPORT = b'<html><h1>FORM 8-K</h1><h2>Item 8.01 Other Events</h2><p>Trust issued notes under a registered offering.</p><h2>Item 9.01 Exhibits</h2></html>'


def _detail_url(acc, cik):
    return f'https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace("-", "")}/{acc}-index.htm'


def _sgml(filename):
    return (f'<SEC-DOCUMENT>\n<DOCUMENT>\n<TYPE>EX-99\n<SEQUENCE>2\n<FILENAME>ex99.htm\n<TEXT>other</TEXT></DOCUMENT>\n'
            f'<DOCUMENT>\n<TYPE>8-K\n<SEQUENCE>1\n<FILENAME>{filename}\n<TEXT><html>primary</html></TEXT></DOCUMENT>').encode()


@pytest.mark.parametrize('acc,cik,filename', KNOWN_FILINGS)
def test_real_accession_shapes_resolved_through_official_submission_headers(acc,cik,filename):
    url = _detail_url(acc,cik)
    assert complete_submission_url(url,acc).endswith('/'+acc+'.txt')
    assert primary_document_from_submission(_sgml(filename),url,'8-K').endswith('/'+filename)


@pytest.mark.parametrize('acc,cik,filename', KNOWN_FILINGS)
def test_end_to_end_primary_fallback_and_retry_dedup(acc,cik,filename):
    detail = _detail_url(acc,cik)
    primary = detail.rsplit('/',1)[0]+'/'+filename
    submission = complete_submission_url(detail,acc)
    class Client:
        request_count = 0
        def __init__(self):
            self.urls = []
        def get(self,url):
            self.urls.append(url)
            self.request_count += 1
            if url == detail:
                return b'<html><title>Filing Detail</title><body>Document grid missing</body></html>'
            if url == submission:
                return _sgml(filename)
            assert url == primary
            return REPORT

    form = Filing(acc,'8-K',cik.zfill(10),'Trust filer','2026-10-16','2026-10-16T14:00:00Z','atom_updated',detail,'',('8.01',),'atom')
    with TemporaryDirectory() as folder:
        path = Path(folder)/'edgar.sqlite3'
        with connect(path) as db:
            assert add_form(db,form) == 'inserted'
            client = Client()
            result = process_forms(db,client,path,limit=0)
            assert result['errors'] == 0 and result['downloaded'] == 1 and result['parsed'] == 1
            assert client.urls == [detail, submission, primary]
            row = db.execute('SELECT status,primary_url,file_guid FROM forms WHERE index_url=?',(detail,)).fetchone()
            assert row['status'] == 'parsed' and row['primary_url'] == primary and row['file_guid']
            assert db.execute('SELECT COUNT(*) FROM items WHERE form_id=(SELECT id FROM forms WHERE index_url=?)',(detail,)).fetchone()[0] == 1
            assert process_forms(db,client,path,limit=0)['selected'] == 0
            assert client.request_count == 3


def test_document_table_variant_has_validated_primary_type():
    acc,cik,_ = KNOWN_FILINGS[0]
    url = _detail_url(acc,cik)
    html = b'''<table class="alternate-layout"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
       <tr><td>2</td><td>EX</td><td><a href="attachment.htm">exhibit</a></td><td>EX-99</td><td>7</td></tr>
       <tr><td>1</td><td>main report</td><td><a href="tm2626710d25_8k.htm">8K</a></td><td>8-K</td><td>900</td></tr></table>'''
    assert primary_document_url(html,url,'8-K').endswith('tm2626710d25_8k.htm')


def test_sec_access_denied_response_has_explicit_diagnostic():
    assert 'SEC access' in sec_access_problem(b'<html>Request Rate Threshold Exceeded</html>')
    assert sec_access_problem(b'<html>Filing Detail</html>') is None


def test_rejects_wrong_accession_and_malicious_primary_path():
    acc,cik,_ = KNOWN_FILINGS[0]
    url = _detail_url(acc,cik)
    with pytest.raises(ValueError,match='does not match'):
        complete_submission_url(url,KNOWN_FILINGS[2][0])
    bad = _sgml('../other_folder.htm')
    with pytest.raises(ValueError,match='No valid primary'):
        primary_document_from_submission(bad,url,'8-K')
    bad = _sgml('attachment.pdf')
    with pytest.raises(ValueError,match='No valid primary'):
        primary_document_from_submission(bad,url,'8-K')
