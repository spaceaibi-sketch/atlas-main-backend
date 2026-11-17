"""Audit/rebuild mobile and 7-day company impact caches from saved assessments.

Default is read-only. --apply uses an online SQLite backup *before* changes,
then recalculates from existing AI scores without calling OpenAI or changing any
assessment/status. Designed to help diagnose a missing Android main-page score.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dashboard.company_analytics import WINDOW_SECONDS, _evidence_cte, refresh_company_aggregates
from dashboard.mobile_summary import refresh_company_summaries
from sec_edgar.database import backup_database
from sec_edgar.settings import database_path


def _table_exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def audit(db, *, now=None):
    """Compare the two materialized stores to completed source scores in set-based SQL."""
    high = int((now or datetime.now(timezone.utc)).timestamp())
    has_summary = _table_exists(db, 'summary')
    # Mobile API's raw score is a signed sum of every completed SEC/News
    # assessment, not the companies table's seven-day weighted total.
    source_sql = '''WITH source_scores AS (
        SELECT f.company_id, i.ai_impact impact
        FROM items i JOIN forms f ON f.id=i.form_id WHERE i.status='analysed'
    '''
    if _table_exists(db, 'news') and _table_exists(db, 'news_company_relations'):
        source_sql += " UNION ALL SELECT r.company_id, r.ai_impact FROM news_company_relations r WHERE r.status='analysed' AND r.company_id IS NOT NULL"
    source_sql += '''), raw AS (
        SELECT company_id, SUM(impact) score, COUNT(*) scored FROM source_scores GROUP BY company_id
    ) SELECT c.id company_id,c.ticker,c.name,
           COALESCE(raw.scored,0) assessed_count,raw.score expected_score,
    '''
    if has_summary:
        source_sql += '''s.aggregate_score stored_score,s.company_id summary_company_id,
            s.refreshed_at refreshed_at
            FROM companies c LEFT JOIN raw ON raw.company_id=c.id
            LEFT JOIN summary s ON s.company_id=c.id'''
    else:
        source_sql += '''NULL stored_score,NULL summary_company_id,NULL refreshed_at
            FROM companies c LEFT JOIN raw ON raw.company_id=c.id'''
    mobile = [dict(r) for r in db.execute(source_sql)]
    for row in mobile:
        expected, stored = row['expected_score'], row['stored_score']
        row['mobile_mismatch'] = (row['summary_company_id'] is None or
                                  (expected is None) != (stored is None) or
                                  (expected is not None and abs(expected-stored) > 1e-7))
    # The seven-day weighted cache is separate and decays with UTC passage.
    weighted_sql = _evidence_cte(db) + ''' , weighted AS (
        SELECT company_id,
        ROUND(SUM(impact * age_weight * confidence / 10.0), 4) impact_total,
        ROUND(SUM(monetary * age_weight), 2) monetary_total,
        ROUND(SUM(confidence * age_weight) / NULLIF(SUM(age_weight),0),4) confidence_avg
        FROM recent GROUP BY company_id
    ) SELECT c.id company_id,c.acumm_impact,c.acumm_monetary,c.accum_confdent,
      COALESCE(w.impact_total,0) expected_impact,
      COALESCE(w.monetary_total,0) expected_monetary,
      COALESCE(w.confidence_avg,0) expected_confidence
    FROM companies c LEFT JOIN weighted w ON w.company_id=c.id'''
    weighted = {r['company_id']: dict(r) for r in db.execute(weighted_sql, {'high':high,'low':high-WINDOW_SECONDS})}
    mobile_bad = weighted_bad = 0
    result = []
    for row in mobile:
        other = weighted[row['company_id']]
        row.update(other)
        row['seven_day_mismatch'] = any(abs((row[stored] or 0)-(row[expected] or 0)) > tol for stored,expected,tol in (
            ('acumm_impact','expected_impact',1e-4),('acumm_monetary','expected_monetary',1e-2),
            ('accum_confdent','expected_confidence',1e-4)))
        mobile_bad += row['mobile_mismatch']
        weighted_bad += row['seven_day_mismatch']
        if row['mobile_mismatch'] or row['seven_day_mismatch']:
            result.append(row)
    return {
        'as_of_utc':datetime.fromtimestamp(high,timezone.utc).isoformat(),
        'companies_checked':len(mobile),'mobile_summary_missing':not has_summary,
        'mobile_mismatched':mobile_bad,'seven_day_mismatched':weighted_bad,
        'companies_with_any_mismatch':len(result),
        'scored_company_count':sum(x['assessed_count'] > 0 for x in mobile),
        'mismatches':result,
    }


def main(argv=None):
    p=argparse.ArgumentParser(description='Dry-run audit or rebuild company score caches from saved AI assessments')
    p.add_argument('--db',type=Path,default=None)
    p.add_argument('--apply',action='store_true',help='Back up SQLite and rebuild caches; no AI rescoring')
    p.add_argument('--report-dir',type=Path,default=None,help='Write JSON and CSV reports to directory')
    args=p.parse_args(argv)
    db_path=(args.db or database_path()).resolve()
    if not db_path.exists():
        p.error(f'Database not found: {db_path}')
    conn=sqlite3.connect(f'file:{db_path}?mode={"rw" if args.apply else "ro"}',uri=True,timeout=30)
    conn.row_factory=sqlite3.Row
    try:
        before=audit(conn)
        backup=None
        if args.apply:
            # Use online SQLite backup (includes WAL) rather than raw file copy.
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup=db_path.with_name(db_path.name+f'.before_score_reconcile_{stamp}.bak')
            if backup.exists():
                raise FileExistsError(f'Backup already exists: {backup}')
            backup_database(conn,backup)
            refresh_company_aggregates(conn)
            refresh_company_summaries(conn)
        after=audit(conn) if args.apply else None
    finally:
        conn.close()
    summary={'mode':'apply' if args.apply else 'dry_run',
             'db':str(db_path),'backup':str(backup) if backup else None,
             'before':{k:v for k,v in before.items() if k!='mismatches'},
             'after':{k:v for k,v in after.items() if k!='mismatches'} if after else None}
    if args.report_dir:
        args.report_dir.mkdir(parents=True,exist_ok=True)
        (args.report_dir/'score_reconciliation.json').write_text(json.dumps({**summary,'mismatches_before':before['mismatches'],
             'mismatches_after':after['mismatches'] if after else None},indent=2,ensure_ascii=False),encoding='utf-8')
        with (args.report_dir/'score_reconciliation.csv').open('w',newline='',encoding='utf-8') as out:
            if before['mismatches']:
                writer=csv.DictWriter(out,fieldnames=list(before['mismatches'][0]))
                writer.writeheader();writer.writerows(before['mismatches'])
            else:
                out.write('company_id,mobile_mismatch,seven_day_mismatch\n')
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
