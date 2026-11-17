"""Regression coverage: SEC metadata collection has no age cutoff."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET

from sec_edgar.collector import Collector, FEED_URL
from sec_edgar.database import connect
from sec_edgar.cli import main as metadata_main

NS = 'http://www.w3.org/2005/Atom'
ET.register_namespace('', NS)


def feed(start, count, *, invalid_first=False):
    root = ET.Element(f'{{{NS}}}feed')
    for offset in range(count):
        n = start + offset
        entry = ET.SubElement(root, f'{{{NS}}}entry')
        ET.SubElement(entry, f'{{{NS}}}category', term='10-K')
        ET.SubElement(entry, f'{{{NS}}}title').text = '10-K - Old Company (0000123456) (Filer)'
        if not (invalid_first and offset == 0):
            accession = f'0000123456-19-{n:06d}'
            ET.SubElement(entry, f'{{{NS}}}id').text = f'urn:tag:sec.gov,2008:accession-number={accession}'
        ET.SubElement(entry, f'{{{NS}}}updated').text = '2019-01-15T08:00:00Z'
        ET.SubElement(entry, f'{{{NS}}}link', href=f'https://www.sec.gov/Archives/edgar/data/123456/000012345619{n:06d}/0000123456-19-{n:06d}-index.htm')
        ET.SubElement(entry, f'{{{NS}}}summary').text = 'Filed: 2019-01-15'
    return ET.tostring(root, encoding='utf-8')


def test_old_filings_including_more_pages_are_collected_and_deduplicated():
    class Client:
        def __init__(self): self.calls = []
        def get(self, url, *, params):
            assert url == FEED_URL
            self.calls.append((params['type'], params['start']))
            if params['type'] != '10-K':
                return feed(0, 0)
            if params['start'] == 0:
                return feed(0, 100)
            if params['start'] == 100:
                return feed(100, 2)
            raise AssertionError('Should stop after the short SEC page')
    with TemporaryDirectory() as temp:
        with connect(Path(temp) / 'edgar.sqlite3') as db:
            client = Client()
            collector = Collector(db, client, hours=1, now=datetime(2026, 10, 17, tzinfo=timezone.utc))
            collector.live_atom()
            assert collector.stats['scanned'] == 102
            assert collector.stats['inserted'] == 102
            assert ('10-K', 100) in client.calls
            assert collector.stats['atom_pages'] == 4
            assert collector.stats['errors'] == []
            again = Collector(db, client)
            again.live_atom()
            assert again.stats['inserted'] == 0
            assert again.stats['already_seen'] == 102
            assert db.execute('SELECT COUNT(*) FROM forms').fetchone()[0] == 102


def test_invalid_atom_entry_does_not_prematurely_stop_paging():
    class Client:
        def get(self, url, *, params):
            if params['type'] != '10-K':
                return feed(0, 0)
            return feed(0, 100, invalid_first=True) if params['start'] == 0 else feed(101, 1)
    with TemporaryDirectory() as temp:
        with connect(Path(temp) / 'edgar.sqlite3') as db:
            result = Collector(db, Client())
            result.live_atom()
            assert result.stats['inserted'] == 100  # 99 + 1
            assert result.stats['atom_pages'] == 4


def test_replayed_atom_page_stops_safely():
    class Client:
        def get(self, url, *, params):
            return feed(0, 100) if params['type'] == '10-K' else feed(0, 0)
    with TemporaryDirectory() as temp:
        with connect(Path(temp) / 'edgar.sqlite3') as db:
            result = Collector(db, Client())
            result.live_atom()
            assert result.stats['inserted'] == 100
            assert any('repeated an earlier page' in x for x in result.stats['errors'])


def test_cli_deprecated_hours_does_not_filter_historical_entries():
    with TemporaryDirectory() as temp:
        dbpath = Path(temp) / 'edgar.sqlite3'
        fixture = Path(temp) / 'old.atom'
        fixture.write_bytes(feed(0, 1))
        assert metadata_main(['--db', str(dbpath), '--import-atom', str(fixture), '--hours', '1',
                              '--now', '2026-10-17T00:00:00Z']) == 0
        with connect(dbpath) as db:
            assert db.execute('SELECT COUNT(*) FROM forms').fetchone()[0] == 1
