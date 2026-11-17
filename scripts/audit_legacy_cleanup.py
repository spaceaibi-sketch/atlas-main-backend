#!/usr/bin/env python3
"""Read-only, fail-closed check before removing Atlas legacy source tables.

It deliberately NEVER drops tables. Old News/SEC ingestion and scoring writers
still require their original tables, so deleting them in this transition would
stop production ingestion and lose new events.

Usage:
  python3 scripts/audit_legacy_cleanup.py --db /opt/atlas/data/edgar.sqlite3
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import sqlite3

LEGACY = ('news', 'forms', 'items', 'news_company_relations',
          'company_mobile_summary', 'company_event_impact_history')
CANONICAL = ('companies', 'raw_items', 'relationships', 'summary')
SQL_LEGACY = re.compile(
    r'\b(?:FROM|JOIN|UPDATE|INTO|TABLE|ON|DELETE\s+FROM|DROP\s+TABLE)\s+'
    r'(?:IF\s+(?:NOT\s+)?EXISTS\s+)?'
    r'(?:["`\[])?(?:news_company_relations|company_mobile_summary|'
    r'company_event_impact_history|news|forms|items)\b', re.IGNORECASE)


def active_legacy_sql_files(root: Path) -> list[str]:
    paths = []
    for directory in ('ai','news_pipeline','sec_edgar','dashboard',
                      'price_pipeline','shared'):
        p = root/directory
        if not p.exists():
            continue
        for file in sorted(p.rglob('*.py')):
            # The bridge is a known dependency and must itself be retired
            # before the physical source tables can be removed.
            code = file.read_text(encoding='utf-8', errors='replace')
            if SQL_LEGACY.search(code):
                paths.append(str(file.relative_to(root)))
    app = root/'app.py'
    if app.is_file() and SQL_LEGACY.search(app.read_text(encoding='utf-8', errors='replace')):
        paths.append('app.py')
    return paths


def audit(db: sqlite3.Connection, root: Path) -> bool:
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print('Canonical tables:', {name: name in tables for name in CANONICAL})
    print('Legacy source rows:')
    for name in LEGACY:
        if name in tables:
            n=db.execute('SELECT COUNT(*) FROM "'+name+'"').fetchone()[0]
            print(f'  {name}: {n:,} rows')
    bridges=db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_unified_%' ORDER BY name").fetchall()
    modules=active_legacy_sql_files(root)
    print('Legacy synchronization triggers:', len(bridges))
    print('Production Python modules with legacy SQL:')
    for name in modules:
        print('  '+name)
    if not all(name in tables for name in CANONICAL):
        print('BLOCKED: one or more canonical tables are missing.')
    if modules or bridges:
        print('BLOCKED: current ingestion/scoring/dashboard code still depends on legacy source tables.')
        print('Do NOT drop legacy tables until these writers/readers are converted and tested.')
    return not modules and not bridges and all(t in tables for t in CANONICAL)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True, type=Path)
    p.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    args=p.parse_args()
    uri=args.db.expanduser().resolve(strict=True).as_uri()+'?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        ready=audit(db,args.source_root.resolve())
    if ready:
        print('Source-file audit passed; verify other services and data parity before planning table removal.')
    return 0 if ready else 2

if __name__=='__main__':
    raise SystemExit(main())
