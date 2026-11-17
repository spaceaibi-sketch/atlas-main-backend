"""Read the supplied curated RSS CSV and parse both RSS and Atom safely."""
from __future__ import annotations
import csv
import html
import re
from dataclasses import dataclass
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
SOURCE_CSV=ROOT/'assets'/'news_sources.csv'

@dataclass(frozen=True)
class Story:
    title: str
    url: str
    summary: str
    published_utc: str
    source: str
    region: str
    category: str


def load_sources(path:Path=SOURCE_CSV):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    return [r for r in rows if r.get('type','').lower()=='rss' and r.get('url','').startswith('https://')]


def clean_text(value):
    soup=BeautifulSoup(html.unescape(value or ''),'html.parser')
    for node in soup(['script','style','iframe']):node.decompose()
    return re.sub(r'\s+',' ',soup.get_text(' ',strip=True)).strip()


def utc_time(raw: str):
    if raw:
        try:
            dt=parsedate_to_datetime(raw)
        except (TypeError,ValueError,IndexError):
            try:dt=datetime.fromisoformat(raw.replace('Z','+00:00'))
            except ValueError:dt=None
        if dt:
            if dt.tzinfo is None or dt.utcoffset() is None:
                return ''  # Ambiguous feed time: never assume server-local or UTC.
            return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    # Unknown feed publication time is not a fabricated published-at timestamp.
    return ''


def parse_feed(payload:bytes,source:dict, *, max_entries=30):
    if len(payload)>3_000_000:raise ValueError('RSS response too large')
    root=ET.fromstring(payload)
    entries=[]
    if root.tag.lower().endswith('rss') or root.find('channel') is not None:
        nodes=root.findall('./channel/item')
        atom=False
    else:
        nodes=root.findall('{http://www.w3.org/2005/Atom}entry')
        atom=True
    for item in nodes:
        if atom:
            ns='{http://www.w3.org/2005/Atom}'
            title=item.findtext(ns+'title') or ''
            link=next((n.get('href') for n in item.findall(ns+'link') if n.get('rel','alternate')=='alternate'), '')
            summary=item.findtext(ns+'summary') or item.findtext(ns+'content') or ''
            published=item.findtext(ns+'published') or ''
        else:
            title=item.findtext('title') or ''
            link=item.findtext('link') or ''
            summary=item.findtext('description') or ''
            published=item.findtext('pubDate') or item.findtext('{http://purl.org/dc/elements/1.1/}date') or ''
        url=urljoin(source['url'],(link or '').strip())
        title=clean_text(title)
        if not title or not url.startswith('https://'):continue
        entries.append(Story(title=title[:700],url=url,summary=clean_text(summary)[:4500],
            published_utc=utc_time(published),source=source['name'],
            region=source.get('region',''),category=source.get('category','')))
        if max_entries > 0 and len(entries) >= max_entries:
            break
    return entries
