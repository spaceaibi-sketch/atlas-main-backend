"""Deterministic, conservative news deduplication across RSS publishers.

Avoids merging different developments just because they discuss one company.
"""
from __future__ import annotations
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from difflib import SequenceMatcher

TRACKING = {'fbclid','gclid','gbraid','wbraid','mc_cid','mc_eid','igshid','ref','ref_src','source','cmpid'}
SOURCES = {'reuters','associated press','ap news','bloomberg','cnn','cnbc','bbc','the guardian','techcrunch','marketwatch','yahoo finance','financial times','al jazeera'}


def normalized_url(value: str) -> str:
    p=urlsplit((value or '').strip())
    host=(p.hostname or '').lower().removeprefix('www.')
    path=re.sub('/+', '/', p.path or '/').rstrip('/') or '/'
    query=urlencode(sorted((k,v) for k,v in parse_qsl(p.query,keep_blank_values=True)
        if not k.lower().startswith('utm_') and k.lower() not in TRACKING))
    return urlunsplit(('https',host,path,query,'')) if host else ''


def normalized_title(value: str) -> str:
    text=unicodedata.normalize('NFKC', value or '').casefold()
    text=re.sub(r'\s+(?:[-|–—])\s+([^|–—]{2,45})$', lambda m: '' if m.group(1).strip().casefold() in SOURCES else m.group(0), text)
    text=re.sub(r'[^\w\s]', ' ', text,flags=re.UNICODE)
    return ' '.join(text.split())


def story_hash(title: str) -> str:
    return hashlib.sha256(normalized_title(title).encode('utf-8')).hexdigest()


def near_duplicate(title: str, old_title: str, *, day: str, old_day: str) -> bool:
    if day != old_day: return False
    a,b=normalized_title(title),normalized_title(old_title)
    if not a or not b or len(a)<35 or len(b)<35: return False
    wa,wb=set(a.split()),set(b.split())
    # Require almost exact wording and high token overlap, not just a shared entity.
    return (len(wa & wb)/max(len(wa | wb),1) >= .90
            and SequenceMatcher(None,a,b).ratio() >= .94)
