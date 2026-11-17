from shared.processing_log import log_step
"""Collect all currently exposed SEC Atom filing metadata into companies/forms.

Only exact 8-K, 10-K, and 10-Q are considered. No age cutoff is applied:
all available feed pages are visited until the feed ends or the configured
page safety limit is reached. The database still rejects duplicate accessions.
Primary documents and Item text are handled by extract_forms_items.
"""
import logging
from xml.etree import ElementTree

from .parsing import parse_atom
from .database import add_form
from .settings import FORMS
from .raw_archive import archive_bytes

LOG = logging.getLogger(__name__)
FEED_URL = 'https://www.sec.gov/cgi-bin/browse-edgar'
ATOM_ENTRY = '{http://www.w3.org/2005/Atom}entry'
PAGE_SIZE = 100


class Collector:
    def __init__(self, db, client=None, *, hours=None, max_pages=100, now=None, db_path=None):
        # `hours`/`now` are accepted for existing Python integrations only.
        # They intentionally do NOT limit the age of filings anymore.
        self.db = db
        self.client = client
        self.db_path = db_path or db.execute('PRAGMA database_list').fetchone()[2] or None
        self.max_pages = max_pages
        self.stats = {
            'scanned': 0, 'inserted': 0, 'already_seen': 0,
            'excluded': 0, 'atom_pages': 0, 'errors': [],
        }

    def add(self, filing):
        self.stats['scanned'] += 1
        log_step('sec.atom','candidate',form=filing.form_type,accession=filing.accession,cik=filing.cik,company=filing.company_name,item_types=list(filing.items))
        result = add_form(self.db, filing)
        self.stats[result] += 1
        if result == 'inserted':
            LOG.info('NEW %-4s %s %s', filing.form_type, filing.accession,
                     filing.company_name)

    def import_atom(self, content, *, origin_url="offline_atom_import"):
        """Offline import also includes old filings (no time window)."""
        if self.db_path is not None:
            archive_bytes(self.db_path,service='sec_atom',subject='offline_import',
                          kind='feed',url=origin_url,payload=content,extension='.xml')
        log_step('sec.atom','offline_received',source=origin_url,bytes=len(content))
        for filing in parse_atom(content):
            if filing.form_type in FORMS:
                self.add(filing)

    def live_atom(self):
        if self.client is None:
            raise ValueError('SEC HTTP client required for live collection')
        for form in FORMS:
            page_signatures = set()
            for page in range(self.max_pages):
                start = page * PAGE_SIZE
                try:
                    log_step('sec.atom','request_page',form=form,page=page,offset=start,page_size=PAGE_SIZE)
                    content = self.client.get(FEED_URL, params={
                        'action': 'getcurrent', 'type': form, 'count': PAGE_SIZE,
                        'start': start, 'owner': 'exclude', 'output': 'atom',
                    })
                    if self.db_path is not None:
                        archive_bytes(self.db_path, service='sec_atom', subject=form,
                            kind=f'offset_{start}', url=FEED_URL,
                            payload=content, extension='.xml',
                            metadata={'form_type':form,'offset':start,'count':PAGE_SIZE})
                    # Use the ACTUAL Atom entry count for paging, not the
                    # subset that our parser could successfully interpret.
                    root = ElementTree.fromstring(content)
                    raw_count = len(root.findall(ATOM_ENTRY))
                    entries = parse_atom(content)
                except Exception as exc:
                    message = (f'Atom {form} start={start}: '
                               f'{type(exc).__name__}: {str(exc)[:200]}')
                    LOG.error('%s', message)
                    log_step('sec.atom','page_failed',form=form,offset=start,reason=message,level='error')
                    self.stats['errors'].append(message)
                    break
                log_step('sec.atom','page_parsed',form=form,offset=start,raw_entries=raw_count,valid_entries=len(entries))
                self.stats['atom_pages'] += 1
                # A server or proxy ignoring `start` could replay page 0;
                # stop safely rather than requesting 100 identical pages.
                signature = tuple(f.accession for f in entries)
                if raw_count and signature in page_signatures:
                    message = (f'Atom {form} offset={start}: SEC repeated an '
                               'earlier page; stopped to avoid endless pagination')
                    LOG.warning('%s', message)
                    self.stats['errors'].append(message)
                    break
                page_signatures.add(signature)
                before = {name: self.stats[name]
                          for name in ('inserted', 'already_seen', 'excluded')}
                for filing in entries:
                    if filing.form_type in FORMS:
                        self.add(filing)
                new = self.stats['inserted'] - before['inserted']
                seen = self.stats['already_seen'] - before['already_seen']
                excluded = self.stats['excluded'] - before['excluded']
                LOG.info('ATOM %-4s offset=%d returned=%d valid=%d new=%d '
                         'already_seen=%d excluded=%d', form, start, raw_count,
                         len(entries), new, seen, excluded)
                if raw_count < PAGE_SIZE:
                    break
            else:
                message = (f'Atom {form}: maximum pagination '
                           f'({self.max_pages} pages) reached; feed may be incomplete')
                LOG.warning('%s', message)
                self.stats['errors'].append(message)
