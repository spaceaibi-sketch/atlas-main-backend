"""Reconcile saved AI-extracted names with the SEC catalog; never calls an AI model.

Default = preview only. --apply inserts missing uniquely matched SEC company
candidate relations without overwriting old matches or assessments. JSON + CSV reports are
written in both modes. Does not modify news.companies or skipped articles.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from sec_edgar.database import now_iso
from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import extraction_lock, load_env
from .matching import SecCompanyMatcher


def _name_list(raw):
    # A Python iterator is appropriate here: varying-length names, rather than
    # fixed-shape numeric arrays, require Unicode string normalization/matching.
    unique = set()
    for segment in (raw or '').split('|'):
        name = segment.strip()
        key = name.casefold()
        if name and key not in unique:
            unique.add(key)
            yield name


def plan(conn, *, exposures=None, ceos=None):
    """Build a complete deterministic report. No SQL writes performed."""
    matcher = SecCompanyMatcher(conn)
    rows = [dict(r) for r in conn.execute('''
        SELECT news_id,title,summary,article_text,companies,status
        FROM atlas_articles
        WHERE status IN ('extracted','connected','analysed')
          AND NULLIF(TRIM(companies),'') IS NOT NULL
        ORDER BY news_id''')]
    existing = {(r['news_id'], r['company_id']) for r in conn.execute('''
        SELECT news_id,company_id FROM atlas_links
        WHERE company_id IS NOT NULL''')}
    details = []
    seen_proposed = set()
    for row in rows:
        nid = row['news_id']
        for name in _name_list(row['companies']):
            found, method = matcher.lookup(name)
            candidates = sorted(found, key=lambda x: x[0])
            detail = dict(news_id=nid, news_status=row['status'], extracted_name=name,
                          match_method=method, outcome='', company_id=None,
                          cik='', sec_name='', candidates='')
            if len(candidates) > 1:
                detail['outcome'] = 'ambiguous'
                detail['candidates'] = ' | '.join(f'{c[0]}:{c[2]}' for c in candidates)
            elif not candidates:
                detail['outcome'] = ('unsafe_partial' if method == 'unsafe_partial'
                                     else 'unmatched')
            else:
                cid,cik,official = candidates[0]
                detail.update(company_id=cid, cik=cik, sec_name=official)
                if (nid, cid) in existing:
                    detail['outcome'] = 'already_linked'
                else:
                    # An article may contain multiple aliases for the same SEC
                    # company; a set avoids scanning all previously seen rows.
                    if (nid, cid) in seen_proposed:
                        detail['outcome'] = 'duplicate_alias'
                    else:
                        seen_proposed.add((nid, cid))
                        # Identity matching is not evidence validation. The
                        # Phase 2 model evaluates the candidate's relevance.
                        detail['outcome'] = 'would_insert'
            details.append(detail)
    pending = [d for d in details if d['outcome'] == 'would_insert']
    summary = dict(articles_scanned=len(rows), names_scanned=len(details),
                   outcomes=dict(sorted(Counter(d['outcome'] for d in details).items())),
                   eligible_new_links=len(pending), mode='preview')
    return details, summary


def apply_plan(conn, details):
    """Insert only pending relations; preserve existing links and AI results."""
    candidates = [d for d in details if d['outcome'] == 'would_insert']
    created = 0
    affected = set()
    stamp = now_iso()
    with conn:
        for d in candidates:
            nid, cid, cik = d['news_id'], d['company_id'], d['cik']
            # Recheck uniqueness under writer lock, including older alternate entity keys.
            source=conn.execute("""SELECT id FROM raw_items WHERE source_type='news'
              AND source_key=?""",(nid,)).fetchone()
            if not source:
                d['outcome']='missing_source'
                continue
            sid=source['id']
            if conn.execute("""SELECT 1 FROM relationships WHERE source_id=?
                AND company_id=? LIMIT 1""",(sid,cid)).fetchone():
                d['outcome']='already_linked'
                continue
            cursor=conn.execute("""INSERT OR IGNORE INTO relationships
                (source_id,company_id,entity_type,entity_key,entity_name,
                 created_at,updated_at,match_hits,match_evidence,correlation_reason)
                VALUES(?,?,'company',?,?,?,?,0,?,?)""",
                (sid,cid,'CIK:'+cik,d['sec_name'],stamp,stamp,
                 'Backfill: unique SEC identity from Phase 1 AI extraction',''))
            if cursor.rowcount:
                d['outcome'] = 'inserted'
                created += 1
                affected.add(nid)
            else:
                d['outcome'] = 'already_linked'
        if affected:
            # Phase 2 selects ONLY relations whose news.status is connected.
            # An analysed story receiving a newly discovered candidate must be
            # reopened for that candidate; existing scored relations are untouched.
            conn.executemany("""UPDATE raw_items SET status='connected',updated_at=?
                WHERE source_type='news' AND source_key=?
                  AND status IN ('created','analysed')""",
                [(stamp,nid) for nid in affected])
    return created


def _write_report(report_dir, details, summary):
    folder = Path(report_dir).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    prefix = folder / f'news_recorrelation_{stamp}'
    json_path = Path(str(prefix)+'.json')
    csv_path = Path(str(prefix)+'.csv')
    json_path.write_text(json.dumps({'summary':summary,'matches':details},ensure_ascii=False,indent=2),encoding='utf-8')
    keys = ['news_id','news_status','extracted_name','match_method','outcome',
            'company_id','cik','sec_name','candidates']
    def csv_safe(value):
        # Prevent company names/articles from being executed as spreadsheet formulae.
        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
            return '\'' + value
        return value
    with csv_path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows({key: csv_safe(d.get(key,'')) for key in keys} for d in details)
    return json_path, csv_path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Recalculate stored News/SEC company matches, without AI calls')
    parser.add_argument('--apply', action='store_true', help='Insert uniquely matched candidate links (default: dry-run)')
    parser.add_argument('--db', type=Path, help='SQLite database (defaults to SEC_DB_PATH or ../data/edgar.sqlite3)')
    parser.add_argument('--report-dir', type=Path, help='Where to write JSON and CSV reports')
    parser.add_argument('--exposures-file', type=Path, help='Deprecated, ignored: Phase 2 is responsible for evidence validation')
    parser.add_argument('--ceos-file', type=Path, help='Deprecated, ignored: CEO registry is not used for deterministic identity matching')
    args = parser.parse_args(argv)
    load_env()
    path = (args.db or database_path()).expanduser().resolve()
    if not path.is_file():
        parser.error(f'Database not found: {path}')
    report_dir = args.report_dir or path.parent/'reports'/'news_recorrelation'
    with extraction_lock(path.parent/'news_pipeline.lock') as locked:
        if not locked:
            parser.error('News collector is running; stop it before recalculating')
        with closing(sqlite3.connect(path,timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('PRAGMA foreign_keys=ON')
            expected={'news','companies','news_company_relations'}
            present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not expected.issubset(present):
                parser.error('Expected News + SEC schema not present; run News migration first')
            details,summary=plan(conn)
            if args.apply:
                backup = path.with_name(path.name + '.before_news_recorrelation_' +
                    datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.bak')
                with closing(sqlite3.connect(backup)) as dst:
                    conn.backup(dst)
                summary['backup'] = str(backup)
                summary['inserted'] = apply_plan(conn,details)
                summary['mode'] = 'applied'
                summary['outcomes'] = dict(sorted(Counter(d['outcome'] for d in details).items()))
            json_path,csv_path = _write_report(report_dir,details,summary)
    print(json.dumps({'summary':summary, 'report_json':str(json_path),
                      'report_csv':str(csv_path)},indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
