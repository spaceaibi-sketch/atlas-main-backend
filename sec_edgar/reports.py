"""Primary SEC document URL discovery and safe local GUID file serving."""
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin, parse_qs, unquote
from bs4 import BeautifulSoup
from .links import secure_sec_url

ALLOWED={'.htm','.html','.xhtml','.txt','.xml'}
UUID_RE=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')

def report_dir(db_path):return Path(db_path).resolve().parent/'reports'

def _safe_report_url(index_url, href):
    """Only primary files in this SEC accession directory may be downloaded."""
    directory = urlsplit(index_url).path.rsplit('/', 1)[0] + '/'
    url = urljoin(index_url, href.strip())
    parts = urlsplit(url)
    if parts.path == '/ix' and parts.netloc.lower() == 'www.sec.gov':
        wrapped = parse_qs(parts.query).get('doc', [])
        if len(wrapped) != 1:
            return None
        url = urljoin('https://www.sec.gov/', wrapped[0])
        parts = urlsplit(url)
    try:
        secure_sec_url(url)
    except ValueError:
        return None
    if not parts.path.startswith(directory):
        return None
    remainder = unquote(parts.path[len(directory):])
    if not remainder or '/' in remainder or "\\" in remainder or remainder in ('.', '..'):
        return None
    if Path(parts.path).suffix.lower() not in ALLOWED:
        return None
    return url


def primary_document_url(index_html, index_url, form):
    """Read the real report link from a SEC filing-detail document table.

    Handles the conventional tableFile class and alternative tables with the
    same SEC document-grid columns. It never selects an attachment as a form.
    """
    secure_sec_url(index_url)
    soup = BeautifulSoup(index_html, 'html.parser')
    for table in soup.find_all('table'):
        headers = [th.get_text(' ', strip=True).lower() for th in table.find_all('th')]
        is_document_table = ('tableFile' in (table.get('class') or [])
                             or ('document' in headers and 'type' in headers))
        if not is_document_table:
            continue
        doc_i = headers.index('document') if 'document' in headers else 2
        type_i = headers.index('type') if 'type' in headers else 3
        for tr in table.find_all('tr'):
            cells = tr.find_all('td', recursive=False)
            if len(cells) <= max(doc_i, type_i):
                continue
            if cells[type_i].get_text(' ', strip=True).upper() != form.upper():
                continue
            link = cells[doc_i].find('a', href=True)
            if not link:
                continue
            result = _safe_report_url(index_url, link['href'])
            if result:
                return result
    raise ValueError(f'Primary {form} report link not found in SEC detail page')


def complete_submission_url(index_url, accession):
    """Official SEC complete-submission SGML file for this accession."""
    secure_sec_url(index_url)
    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession):
        raise ValueError('Invalid SEC accession')
    parts = urlsplit(index_url)
    directory = parts.path.rsplit('/', 1)[0]
    if directory.rsplit('/', 1)[-1] != accession.replace('-', ''):
        raise ValueError('SEC accession directory does not match form accession')
    # Do not silently move between accession directories.
    result = f'https://www.sec.gov{directory}/{accession}.txt'
    secure_sec_url(result)
    return result


def primary_document_from_submission(submission, index_url, form):
    """Find the primary form filename in SEC's own <DOCUMENT> SGML headers.

    Submission files contain <TYPE>8-K, <SEQUENCE>1, <FILENAME>..., <TEXT>...;
    the first matching primary-document header is preferred. Unlike guessing
    filenames from listings, this uses official typed document metadata.
    """
    secure_sec_url(index_url)
    if isinstance(submission, bytes):
        body = submission.decode('latin-1')
    else:
        body = submission
    candidates = []
    for match in re.finditer(r'<DOCUMENT\b[^>]*>(.*?)<TEXT\b[^>]*>', body, re.I | re.S):
        header = match.group(1)
        kind = re.search(r'(?im)^\s*<TYPE>\s*([^\r\n<]+)', header)
        name = re.search(r'(?im)^\s*<FILENAME>\s*([^\r\n<]+)', header)
        if not kind or not name or kind.group(1).strip().upper() != form.upper():
            continue
        sequence = re.search(r'(?im)^\s*<SEQUENCE>\s*(\d+)', header)
        url = _safe_report_url(index_url, name.group(1))
        if url:
            candidates.append((int(sequence.group(1)) if sequence else 10**9, url))
    if not candidates:
        raise ValueError(f'No valid primary {form} document in SEC submission headers')
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def sec_access_problem(content):
    """SEC occasionally serves an informational block page with HTTP 200."""
    prefix = content[:12000].decode('utf-8', errors='replace') if isinstance(content, bytes) else content[:12000]
    words = prefix.lower()
    if 'request rate threshold exceeded' in words or 'your request originates from an undeclared automated tool' in words:
        return 'SEC access/rate-limit notice returned instead of the filing detail page'
    return None

def save_report(db_path,data,url):
    if not data:raise ValueError('Empty primary SEC report')
    suffix=Path(urlsplit(url).path).suffix.lower()
    if suffix not in ALLOWED:raise ValueError('Unsupported SEC report extension')
    guid=str(uuid.uuid4());name=guid+suffix
    directory=report_dir(db_path);directory.mkdir(parents=True,exist_ok=True)
    temp=directory/(name+'.part');final=directory/name
    with temp.open('xb') as f:f.write(data)
    temp.replace(final)
    return guid,name

def local_report_file(conn,db_path,guid):
    if not UUID_RE.fullmatch(guid):return None
    row=conn.execute('SELECT file_name FROM atlas_filings WHERE file_guid=?',(guid,)).fetchone()
    if row is None or not row['file_name']:return None
    directory=report_dir(db_path).resolve();file=(directory/row['file_name']).resolve()
    if file.parent!=directory or not file.name.startswith(guid+'.') or file.suffix.lower() not in ALLOWED or not file.is_file():return None
    mime='text/plain' if file.suffix=='.txt' else 'text/xml' if file.suffix=='.xml' else 'text/html'
    return file,mime
