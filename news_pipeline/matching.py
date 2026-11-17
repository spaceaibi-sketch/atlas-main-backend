"""Resolve AI-proposed company names to unique SEC catalog identities.

Matching order:
  1. Exact normalized full name.
  2. Exact full name after stripping a terminal SEC jurisdiction suffix (/DE).
  3. Token-boundary contains in EITHER direction, only for specific and unique names.

Names are *not* read from article prose here, and an ambiguous match never creates
an SEC relationship. AI-extracted names remain stored independently in news.companies.
"""
from __future__ import annotations

from collections import defaultdict
import re
import unicodedata

from shared.processing_log import log_step

# Standard two-letter US state/territory incorporation suffixes in SEC names.
# Remove them only if separated with a trailing slash: "Example Inc. / DE".
_JURISDICTIONS = set('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR GU VI AS MP'.split())
_SUFFIX = re.compile(r'\s*/\s*([A-Za-z]{2})\s*$')

# A generic name fragment must not identify a corporation merely because the
# SEC catalog happens to contain a unique company with that word in its name.
_UNSAFE_TOKENS = frozenset('''a an and the of for on in to with group groups company companies co corp corporation
inc incorporated ltd limited llc plc holdings holding international global general national
energy power capital market markets technology technologies digital financial finance
bank banks media news paid pay services service systems system solutions industries industry
business enterprises partners properties resources resource communications management
healthcare health medical software hardware products product first new one all'''.split())


def identity_key(name):
    """Ignore punctuation and case while preserving all corporate-name tokens."""
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', str(name or '')).casefold()))


def sec_name_key(name):
    """Normalized name without a terminal SEC state suffix such as '/DE'."""
    text = str(name or '').strip()
    suffix = _SUFFIX.search(text)
    if suffix and suffix.group(1).upper() in _JURISDICTIONS:
        text = text[:suffix.start()].rstrip()
    return identity_key(text)


def _specific_enough(key):
    """Require an identifiable business name, not a generic substring."""
    tokens = key.split()
    meaningful = [token for token in tokens if token not in _UNSAFE_TOKENS]
    if not meaningful:
        return False
    # Single-token company abbreviations/ordinary words are unsafe as partial
    # names. Four or more letters plus a non-generic identity token is needed.
    return len(tokens) > 1 or len(tokens[0]) >= 4


def _contains_words(left, right):
    """True when a full, word-boundary company-name phrase is contained."""
    return bool(right) and f' {right} ' in f' {left} '


class SecCompanyMatcher:
    """Reusable SEC identity index for batch and historical matching.

    Exact matches outrank containment. Containment is checked on normalized
    whole-word phrases in both directions, never arbitrary character fragments.
    Multiple identities remain ambiguous rather than taking the first match.
    """

    def __init__(self, conn):
        self.exact = defaultdict(list)
        self.normalized = defaultdict(list)
        self.token_index = defaultdict(list)
        self.cache = {}
        for row in conn.execute('SELECT id,cik,name FROM companies WHERE name IS NOT NULL'):
            official = row['name']
            entry = (row['id'], row['cik'], official)
            full_key = identity_key(official)
            base_key = sec_name_key(official)
            if not full_key:
                continue
            self.exact[full_key].append(entry)
            self.normalized[base_key].append(entry)
            for token in set(base_key.split()) - _UNSAFE_TOKENS:
                self.token_index[token].append((entry, base_key))

    def lookup(self, company):
        """Return (candidate SEC identities, match method). No silent tie break."""
        full_key = identity_key(company)
        if full_key in self.cache:
            return self.cache[full_key]
        base_key = sec_name_key(company)
        found = self.exact.get(full_key, ())
        if found:
            result = (list(found), 'exact')
        else:
            found = self.normalized.get(base_key, ())
            if found:
                result = (list(found), 'jurisdiction_normalized')
            elif not _specific_enough(base_key):
                result = ([], 'unsafe_partial')
            else:
                matches = {}
                for token in set(base_key.split()) - _UNSAFE_TOKENS:
                    for entry, sec_key in self.token_index.get(token, ()):
                        if (_specific_enough(sec_key) and
                                (_contains_words(base_key, sec_key) or
                                 _contains_words(sec_key, base_key))):
                            matches[entry[0]] = entry
                result = (list(matches.values()), 'bidirectional_contains')
        self.cache[full_key] = result
        return result


def resolve_extracted_companies(conn, extracted):
    """Resolve AI-extracted names; unmatched/ambiguous names remain in news.companies.

    extracted: {news_id: [(AI_full_name, AI_reason), ...]}.
    output: {news_id: [(company_id, cik, official_name, AI_reason), ...]}.
    """
    if not any(pairs for pairs in extracted.values()):
        return {nid: [] for nid in extracted}
    matcher = SecCompanyMatcher(conn)
    resolved = {}
    for nid, pairs in extracted.items():
        entries = []
        seen_ids = set()
        for company, reason in pairs:
            found, method = matcher.lookup(company)
            if len(found) != 1:
                log_step('news.identity', 'unresolved', news_id=nid,
                         proposed_name=company, reason=reason,
                         candidate_count=len(found), match_method=method)
                continue
            cid, cik, official = found[0]
            if cid in seen_ids:
                # Multiple AI aliases must not fail an otherwise valid batch.
                log_step('news.identity', 'alias_duplicate', news_id=nid,
                         proposed_name=company, company_id=cid)
                continue
            seen_ids.add(cid)
            entries.append((cid, cik, official, reason))
            log_step('news.identity', 'matched', news_id=nid,
                     proposed_name=company, company_id=cid, cik=cik,
                     official_name=official, correlation_reason=reason,
                     match_method=method)
        resolved[nid] = entries
    return resolved
