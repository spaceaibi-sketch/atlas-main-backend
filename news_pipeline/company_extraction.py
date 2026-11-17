"""AI-first contextual company extraction; strictly parse ID|Company|Reason lines.

AI only proposes companies. A distinct deterministic step resolves the exact
full corporate name to a unique company already in the local SEC catalog.
No whole-article keyword matching or inferred alias fanout is permitted.
"""
from __future__ import annotations
from shared.processing_log import log_step
from ai.client import configured_model
from shared.config import integer as config_int
from .phase1_request import create_phase1_response
from .response_recovery import unwrap_text
from .matching import identity_key

import json
import logging
import re
from pathlib import Path

LOG = logging.getLogger(__name__)
PROMPT = Path(__file__).with_name('COMPANY_EXTRACTION_PROMPT.md').read_text(encoding='utf-8')
MAX_CONTEXT = config_int('NEWS_COMPANY_CONTEXT_CHARS',1500,minimum=100,maximum=10000)
MAX_REQUEST_BYTES = config_int('NEWS_COMPANY_REQUEST_MAX_BYTES',700_000,minimum=10000,maximum=5000000)


def load_company_catalog(conn) -> tuple[str, dict[str, str]]:
    """Read the complete SEC company catalog once; input is Name|Ticker lines.

    Unique official names are used for normalization when available; unlisted
    AI names remain valid extraction proposals. Tickers are identification
    hints, not proof of company involvement.
    """
    lines = []
    names: dict[str, str | None] = {}
    for name, ticker in conn.execute(
        "SELECT name, ticker FROM companies ORDER BY name COLLATE NOCASE, cik"
    ):
        name = str(name or "").strip()
        ticker = str(ticker or "").strip()
        if not name:
            continue
        if any(ch in name + ticker for ch in "|\r\n"):
            raise ValueError("Invalid company name or ticker for Name|Ticker catalog")
        lines.append(f"{name}|{ticker}")
        key = identity_key(name)
        if key in names:
            names[key] = None  # Ambiguous duplicate official name.
        else:
            names[key] = name
    if not lines:
        raise ValueError("SEC companies table is empty; cannot extract company correlations")
    return "\n".join(lines), {k: v for k, v in names.items() if v is not None}


def parse_lines(raw: str, by_id: dict[int, str], catalog_names: dict[str, str] | None = None,
                rejected_names: list[tuple[str, str]] | None = None,
                failed_articles: dict[str, str] | None = None) -> dict[str, list[tuple[str, str]]]:
    """Read Phase-1 AI decisions without imposing a Python evidence gate.

    With ``failed_articles`` provided, recover valid articles independently:
    invalid, contradictory and omitted article decisions remain pending instead
    of rolling back every valid article in the batch. Without it, keep the
    strict parser API used by direct callers/tests.

    A syntactically valid name need not be capitalized or belong to the SEC
    catalog. Only deterministic identity matching creates SEC relationships.
    """
    tolerant = failed_articles is not None
    if not isinstance(raw, str) or not raw.strip():
        if tolerant:
            failed_articles.update({nid: 'Empty company extraction response' for nid in by_id.values()})
            return {}
        raise ValueError('Empty company extraction response')
    results = {news_id: [] for news_id in by_id.values()}
    seen = set()
    none_ids = set()
    invalid = {}
    unknown_lines = 0
    candidate = unwrap_text(raw)
    if candidate.startswith('```') and candidate.endswith('```'):
        candidate = re.sub(r'^```[^\n]*\n', '', candidate).rsplit('```', 1)[0].strip()

    for line_number, input_line in enumerate(candidate.splitlines(), 1):
        line = input_line.strip()
        if not line:
            continue
        line = re.sub(r'^(?:[-*]\s+|(?:\d+[.)]\s+)(?=\d+\s*\|))', '', line)
        if line.casefold() in ('results:', 'output:', 'extracted companies:'):
            continue
        match = re.match(r'^(\d+)\s*\|', line, re.ASCII)
        if not match or int(match.group(1)) not in by_id:
            if not tolerant:
                raise ValueError(f'Unknown or malformed extraction row at line {line_number}')
            # A line without an identifiable known ID cannot be safely assigned
            # to another article. That article stays pending if no valid line exists.
            LOG.warning('AI extraction ignored unassignable line=%s content=%r',
                        line_number, line[:160])
            unknown_lines += 1
            continue
        nid = by_id[int(match.group(1))]
        if nid in invalid:
            continue
        parts = [v.strip() for v in line.split('|', 2)]
        error = None
        if len(parts) == 2 and parts[1].casefold() == 'none':
            if results[nid]:
                error = 'Contradictory NONE and company lines'
            else:
                seen.add(nid)
                none_ids.add(nid)
        elif len(parts) != 3:
            error = 'Expected ID|Company|Reason or ID|NONE'
        else:
            _number, company, reason = parts
            if nid in none_ids:
                error = 'Contradictory NONE and company lines'
            elif not company or not reason or company.casefold() == 'none':
                error = 'Missing company name or reason (use ID|NONE for no company)'
            elif (len(company) > 200 or len(reason) > 1000 or
                  any(ord(ch) < 32 for ch in company + reason) or '|' in reason):
                error = 'Invalid field length, control character or delimiter'
            else:
                # Capitalization is not an evidence test: the SEC itself uses
                # names such as AMAZON COM INC. Preserve unsupported names.
                if catalog_names is not None:
                    official = catalog_names.get(identity_key(company))
                    if official is None:
                        if rejected_names is not None:
                            rejected_names.append((nid, company))
                    else:
                        company = official
                same = [(old, old_reason) for old, old_reason in results[nid]
                        if old.casefold() == company.casefold()]
                if same:
                    if len(same) != 1 or same[0][1] != reason:
                        error = 'Conflicting repeated company lines'
                else:
                    results[nid].append((company, reason))
                    seen.add(nid)
        if error:
            if not tolerant:
                raise ValueError(f'Article {nid}: {error}')
            invalid[nid] = f'Article {nid}, line {line_number}: {error}'
            LOG.warning('AI extraction decision quarantined: %s', invalid[nid])

    for nid in by_id.values():
        if nid not in seen and nid not in invalid:
            invalid[nid] = 'AI omitted the article decision'
    if invalid and not tolerant:
        sample = next(iter(invalid.items()))
        raise ValueError(f'Invalid company extraction: {sample[0]}: {sample[1]}')
    if tolerant:
        failed_articles.update(invalid)
        return {nid: pairs for nid, pairs in results.items() if nid not in invalid}
    return results

def prepare_extraction(rows: list[dict], exposures=None, ceos=None, *, company_catalog: str):
    """Compact article content; integer IDs are batch-local, never database IDs."""
    if len(rows) > 500:
        raise ValueError('Company extraction batch exceeds 500 articles')
    by_id = {i: row['news_id'] for i, row in enumerate(rows, start=1)}
    items = [{
        'id': i,
        'title': (row.get('title') or '')[:400],
        'summary_or_body': (row.get('article_text') or row.get('summary') or '')[:MAX_CONTEXT],
        'published_utc': row.get('published_utc') or '',
        'source': (row.get('source') or '')[:100],
    } for i, row in enumerate(rows, 1)]
    # Stable catalog prefix enables caching by providers supporting prompt caching.
    data = {'company_catalog': company_catalog, 'news': items,
            'supplied_exposure_evidence': exposures or [], 'supplied_ceo_evidence': ceos or []}
    def encode():
        return json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    raw = encode()
    if len(raw.encode('utf-8')) > MAX_REQUEST_BYTES:
        for max_chars in (1000, 700, 350, 100, 0):
            for item in items:
                item['summary_or_body'] = item['summary_or_body'][:max_chars]
            raw = encode()
            if len(raw.encode('utf-8')) <= MAX_REQUEST_BYTES:
                break
    if len(raw.encode('utf-8')) > MAX_REQUEST_BYTES:
        raise ValueError('AI extraction context exceeds budget with full company catalog; reduce batch size or exposure registry')
    return raw, by_id


def extract_companies(client, rows: list[dict], model=None, exposures=None, ceos=None, request_recorder=None, *, company_catalog: str, catalog_names: dict[str, str], rejected_names=None, failed_articles=None):
    """One AI request for all main stories in a batch, not one call per company."""
    if not rows:
        return {}
    payload, by_id = prepare_extraction(rows, exposures=exposures, ceos=ceos, company_catalog=company_catalog)
    request = {
        'model': configured_model(model, news=True),
        'instructions': PROMPT,
        'input': payload,
        'text': {'format': {'type': 'text'}},
        'store': False,
    }
    if request_recorder is not None:
        request_recorder('company_extraction', request)
    log_step('news.extraction','request',model=request['model'],news_ids=[r['news_id'] for r in rows])
    response = create_phase1_response(client, request)
    if getattr(response, 'status', 'completed') != 'completed':
        raise ValueError('Company extraction model response incomplete')
    unmatched = []
    parsed = parse_lines(getattr(response, 'output_text', ''), by_id,
                         catalog_names=catalog_names, rejected_names=unmatched,
                         failed_articles=failed_articles)
    # Never log names from a quarantined/invalid article as committed extractions.
    unmatched = [(nid, name) for nid, name in unmatched if nid in parsed]
    for nid, company in unmatched:
        LOG.info('AI COMPANY NOT IN UNIQUE SEC CATALOG: article=%s company=%r; preserved in news.companies, no automatic relation', nid, company)
        log_step('news.identity', 'unlisted_company_preserved', news_id=nid,
                 proposed_name=company, reason='Pending deterministic matching or review')
    if rejected_names is not None:
        rejected_names.extend(unmatched)
    for nid,pairs in parsed.items():
        log_step('news.extraction','proposed',news_id=nid,companies=[{'name':name,'reason':reason} for name,reason in pairs])
    return parsed
