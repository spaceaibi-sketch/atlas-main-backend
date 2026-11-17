#!/usr/bin/env python3
"""Read-only audit or backed-up repair of the canonical Atlas mobile summary.

Run under the same source tree used by the Python writer. SQLite's online
backup preserves WAL content. Do not use old company_mobile_summary migrations.

  python3 scripts/repair_mobile_summary.py --db /opt/atlas/data/edgar.sqlite3
  python3 scripts/repair_mobile_summary.py --db /opt/atlas/data/edgar.sqlite3 --apply
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

# Direct `python3 scripts/repair_mobile_summary.py` runs from the scripts dir.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPECTED = {'companies','raw_items','relationships','summary','price_bars'}


def audit(db):
    """Compare all companies using vectorized SQL; do not sample rows."""
    sql = """WITH source AS (
      SELECT r.company_id,
        MAX(CASE WHEN x.source_type='news' AND x.status IN ('connected','analysed')
                 THEN julianday(x.event_at) END) latest_news,
        MAX(CASE WHEN x.source_type='item' AND x.status!='skipped'
                 THEN julianday(x.event_at) END) latest_item,
        SUM(CASE WHEN x.source_type IN ('news','item') AND x.status!='skipped'
                 THEN r.ai_impact END) impact
      FROM relationships r JOIN raw_items x ON x.id=r.source_id
      WHERE r.company_id IS NOT NULL AND r.entity_type='company'
      GROUP BY r.company_id
    )
    SELECT COUNT(*) total,
       SUM(CASE WHEN s.company_id IS NULL THEN 1 ELSE 0 END) missing,
       SUM(CASE WHEN v.latest_news IS NOT NULL AND
           (s.latest_published IS NULL OR
            ABS(v.latest_news-julianday(s.latest_published))>0.0000001)
           THEN 1 ELSE 0 END) news_mismatch,
       SUM(CASE WHEN v.latest_item IS NOT NULL AND
           (s.latest_item_filed IS NULL OR
            ABS(v.latest_item-julianday(s.latest_item_filed))>0.0000001)
           THEN 1 ELSE 0 END) item_mismatch,
       SUM(CASE WHEN (v.impact IS NULL)!=(s.aggregate_score IS NULL) OR
           (v.impact IS NOT NULL AND s.aggregate_score IS NOT NULL
            AND ABS(v.impact-s.aggregate_score)>0.0001)
           THEN 1 ELSE 0 END) impact_mismatch
    FROM companies c LEFT JOIN summary s ON s.company_id=c.id
    LEFT JOIN source v ON v.company_id=c.id"""
    row=db.execute(sql).fetchone()
    return dict(zip(('companies','missing_summary','news_date_mismatch',
                     'item_date_mismatch','impact_mismatch'),map(int,row)))


def run(path:Path, apply:bool):
    uri=path.as_uri()+'?mode=ro'
    with sqlite3.connect(uri,uri=True,timeout=30) as src:
        names={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if names != EXPECTED:
            raise RuntimeError(f'Unsafe schema: expected five canonical tables; found {sorted(names)}')
        if src.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise RuntimeError('SQLite quick_check failed; no repair attempted')
        before=audit(src)
        print('BEFORE:',before,flush=True)
        if not apply:
            print('READ ONLY: pass --apply to create an online backup and refresh stored News/SEC summary values')
            return 0
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup=path.with_name(path.name+f'.pre-mobile-summary-{stamp}.bak')
        if backup.exists():raise FileExistsError(backup)
        # Keep the source read-only until the backup completes. Do not copy
        # the .db file with shutil; SQLite may have uncheckpointed WAL writes.
        with sqlite3.connect(backup) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Online backup integrity check failed; no changes made')
        print('SQLite online backup:',backup,flush=True)
    # Writer transaction is short and atomic; only cached summary columns change.
    from dashboard.mobile_summary import refresh_company_summaries
    with sqlite3.connect(path,timeout=60) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA busy_timeout=60000')
        db.execute('PRAGMA foreign_keys=ON')
        refresh_company_summaries(db,refresh_prices=False)
        after=audit(db)
    print('AFTER:',after)
    if any(after[k] for k in ('missing_summary','news_date_mismatch',
                              'item_date_mismatch','impact_mismatch')):
        print('WARNING: Some values still differ; check source semantics and timestamps.',file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True,type=Path)
    p.add_argument('--apply',action='store_true',help='Take verified online backup then rebuild the summary')
    args=p.parse_args(argv)
    path=args.db.expanduser().resolve(strict=True)
    if not path.is_file():p.error(f'Not a regular SQLite file: {path}')
    try:return run(path,args.apply)
    except (OSError,sqlite3.Error,RuntimeError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
