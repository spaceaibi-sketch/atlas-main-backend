"""Extract the actual Item sections from saved SEC primary filing documents.

8-K: Item 1.01, Item 5.02, etc. (9.01 omitted by default).
10-K / 10-Q: Part I, Item 1; Part II, Item 1; Item 1A, etc.

These are plain-text extracts of the downloaded report, not SEC XBRL financial facts.
The document remains available unchanged under its UUID filename.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString, Tag

from .settings import ITEM_DESCRIPTIONS

_HEADINGS = re.compile(
    r'(?im)^\s*ITEM\s+(\d{1,2}(?:\.\d{2}|[A-Z])?)\s*(?:[.:\-\u2013\u2014]\s*)?([^\n]*)$'
)
_PART = re.compile(r'(?im)^\s*PART\s+(I{1,3}|IV)\b[^\n]*$')
_END_8K = re.compile(r'(?im)^\s*(?:SIGNATURES?|EXHIBIT INDEX)\s*$')
_SPACES = re.compile(r'[ \t\u00a0\u200b]+')
_BLOCK = {'p', 'div', 'tr', 'table', 'section', 'article', 'h1', 'h2', 'h3',
          'h4', 'h5', 'h6', 'li', 'ul', 'ol', 'blockquote', 'pre', 'dl', 'dt', 'dd', 'hr'}


@dataclass(frozen=True)
class ParsedItem:
    item_code: str
    part: str
    item_name: str
    item_text: str


def _clean_lines(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = _SPACES.sub(' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def document_plain_text(data: bytes | str, suffix: str = '.htm') -> str:
    """Linear DOM walk to retain readable paragraph/table row boundaries.

    Per-node iteration is necessary: HTML has irregular nested elements and is
    not rectangular data suitable for vectorization.
    """
    if suffix.lower() == '.pdf' or (isinstance(data, bytes) and data.startswith(b'%PDF')):
        raise ValueError('PDF report cannot be parsed as HTML; no OCR attempted')
    if suffix.lower() == '.txt':
        text = data.decode('utf-8-sig', errors='replace') if isinstance(data, bytes) else data
        return _clean_lines(text)

    soup = BeautifulSoup(data, 'html.parser')
    for tag in soup.find_all(True):
        # A parent may have been decomposed in an earlier iteration. BeautifulSoup
        # clears attributes on all its descendants; touching .get() on such a
        # detached tag raises AttributeError: 'NoneType' object has no attribute 'get'.
        if tag.name is None or tag.attrs is None:
            continue
        if tag.name in {'script', 'style', 'noscript', 'svg', 'ix:header', 'ix:hidden', 'ix:resources'}:
            tag.decompose()
        elif tag.get('aria-hidden') == 'true' or re.search(r'display\s*:\s*none', tag.get('style', ''), re.I):
            tag.decompose()
    # The parser is intentionally text-first; SEC documents often use complex
    # tables for paragraph layout instead of semantic heading elements.
    parent = soup.body if soup.body is not None else soup
    parts = []
    for node in parent.descendants:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag):
            if node.name == 'br' or node.name in _BLOCK:
                parts.append('\n')
            elif node.name in ('td', 'th'):
                parts.append(' ')
    return _clean_lines(''.join(parts))


def parse_document(data: bytes | str, form_type: str, suffix: str = '.htm',
                   *, include_9_01: bool = False) -> list[ParsedItem]:
    """Return report Item sections; deduplicate repeated table-of-contents entries.

    For a repeated (part, item code), retain the candidate with most body text;
    an earlier table-of-contents reference is typically shorter. This is a
    heuristic, and may require verification on unusually formatted filings.
    """
    form_type = form_type.upper()
    if form_type not in ('8-K', '10-K', '10-Q'):
        raise ValueError(f'Unsupported form: {form_type}')
    text = document_plain_text(data, suffix)
    headings = []
    parts = list(_PART.finditer(text)) if form_type != '8-K' else []
    for match in _HEADINGS.finditer(text):
        code = match.group(1).upper()
        if form_type == '8-K' and not re.fullmatch(r'\d{1,2}\.\d{2}', code):
            continue
        if form_type != '8-K' and not re.fullmatch(r'\d{1,2}[A-Z]?', code):
            continue
        part = ''
        if parts:
            # Two PART I headings (TOC + actual report) are harmless here;
            # the longer section usually wins on duplicate headings.
            earlier = [p for p in parts if p.start() <= match.start()]
            if earlier:
                part = 'PART ' + earlier[-1].group(1).upper()
        heading_tail = _clean_lines(match.group(2)).strip(' .:;\t\u2013\u2014-')
        headings.append((match, code, part, heading_tail))
    if not headings:
        return []

    signatures = list(_END_8K.finditer(text)) if form_type == '8-K' else []
    selected = {}
    order = {}
    for i, (match, code, part, heading_tail) in enumerate(headings):
        if form_type == '8-K' and code == '9.01' and not include_9_01:
            continue
        next_start = headings[i + 1][0].start() if i + 1 < len(headings) else len(text)
        # Quarterly/annual Items stop before a new PART heading, not after it.
        # Otherwise the preceding Item may incorrectly include PART II as text.
        if parts:
            next_part = next((p.start() for p in parts if p.start() > match.start()), None)
            if next_part is not None:
                next_start = min(next_start, next_part)
        if signatures:
            next_signature = next((m.start() for m in signatures if m.start() > match.start()), None)
            if next_signature is not None:
                next_start = min(next_start, next_signature)
        body = text[match.start():next_start].strip()
        if not body:
            continue
        name = heading_tail[:250] if heading_tail else ITEM_DESCRIPTIONS.get(code, f'Item {code}')
        # Some EDGAR table-of-contents lines append page numbers: those are
        # usually replaced by longer real sections below.
        value = ParsedItem(code, part, name, body)
        key = (part, code)
        if key not in selected or len(body) > len(selected[key].item_text):
            selected[key] = value
            order[key] = match.start()
    return [selected[k] for k in sorted(selected, key=order.get)]
