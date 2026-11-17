"""SEC Atom metadata parser. Primary report parsing lives in form_parser.py."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone


ATOM = {'a': 'http://www.w3.org/2005/Atom'}
ACCESSION = re.compile(r'\b(\d{10}-\d{2}-\d{6})\b')
ITEM = re.compile(r'\bItem\s+(\d\.\d{2})\s*:', re.I)
CIK_TITLE = re.compile(r'\((\d{10})\)\s*\((?:Filer|Issuer|Subject|Reporting|Filed by)[^)]*\)\s*$', re.I)


def utc_iso(timestamp: datetime) -> str:
    if timestamp.tzinfo is None:
        raise ValueError('Timezone-aware datetime required')
    return timestamp.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_timestamp(value: str) -> datetime:
    obj = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if obj.tzinfo is None:
        raise ValueError(f'Timestamp has no timezone: {value}')
    return obj.astimezone(timezone.utc)


def extract_items(text: str) -> tuple[str, ...]:
    # Unescape HTML first (Atom <summary type="html"> embeds escaped markup).
    return tuple(sorted(set(ITEM.findall(html.unescape(text or '')))))


@dataclass(frozen=True)
class Filing:
    accession: str
    form_type: str
    cik: str
    company_name: str
    filed_date: str
    filing_ts_utc: str
    time_source: str
    index_url: str
    summary: str
    items: tuple[str, ...]
    source: str


def parse_atom(content: bytes | str) -> list[Filing]:
    root = ET.fromstring(content)
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise ValueError('Not an Atom 1.0 feed')
    results: list[Filing] = []
    for entry in root.findall('a:entry', ATOM):
        category = entry.find('a:category', ATOM)
        if category is None or not category.get('term'):
            continue
        form_type = category.attrib['term'].strip()
        title = (entry.findtext('a:title', default='', namespaces=ATOM) or '').strip()
        identifier = entry.findtext('a:id', default='', namespaces=ATOM) or ''
        acc = ACCESSION.search(identifier)
        if not acc:
            continue
        summary = entry.findtext('a:summary', default='', namespaces=ATOM) or ''
        match_date = re.search(r'Filed:\s*</b>\s*(\d{4}-\d{2}-\d{2})', html.unescape(summary), re.I)
        if not match_date:
            match_date = re.search(r'Filed:\s*(\d{4}-\d{2}-\d{2})', html.unescape(summary), re.I)
        timestamp = entry.findtext('a:updated', namespaces=ATOM)
        if not timestamp:
            continue
        stamp = utc_iso(parse_timestamp(timestamp))
        title_match = CIK_TITLE.search(title)
        if title_match:
            cik = title_match.group(1)
            company_name = title[:title_match.start()].split(' - ', 1)[-1].strip()
        else:
            # Safe fallback: get CIK from archive URL if title format differs.
            cik = ''
            company_name = title.split(' - ', 1)[-1].strip()
        link = entry.find('a:link', ATOM)
        href = link.get('href', '') if link is not None else ''
        if not cik:
            cik_match = re.search(r'/data/(\d+)/', href)
            if cik_match:
                cik = cik_match.group(1).zfill(10)
        if not cik or not href.startswith('https://www.sec.gov/Archives/edgar/'):
            continue
        results.append(Filing(
            accession=acc.group(1), form_type=form_type,
            cik=cik, company_name=company_name,
            filed_date=match_date.group(1) if match_date else stamp[:10],
            filing_ts_utc=stamp, time_source='atom_updated',
            index_url=href, summary=summary, items=extract_items(summary),
            source='atom',
        ))
    return results
