"""Read-only verification of Atlas News and SEC Item AI processing states.

Reports exact database counts, never infers missing AI decisions, never runs
OpenAI and never mutates the database. Exit code 2 indicates state anomalies.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import load_env


def audit(db):
    required = ('news','news_company_relations','items','forms')
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = set(required) - present
    if missing:
        raise RuntimeError(f'Missing required tables: {sorted(missing)}')
    def counts(table):
        return {row['status']: row['count'] for row in db.execute(
            f'SELECT status, COUNT(*) AS count FROM {table} GROUP BY status ORDER BY status')}
    sql = {
        'news_analysed_without_completed_relations': '''SELECT COUNT(*) FROM news n
            WHERE n.status='analysed' AND (NOT EXISTS (
                SELECT 1 FROM news_company_relations r WHERE r.news_id=n.news_id)
                OR EXISTS (SELECT 1 FROM news_company_relations r WHERE r.news_id=n.news_id
                           AND r.status!='analysed'))''',
        'news_connected_without_relationships': '''SELECT COUNT(*) FROM news n
            WHERE n.status='connected' AND NOT EXISTS (
                SELECT 1 FROM news_company_relations r WHERE r.news_id=n.news_id)''',
        'news_connected_with_all_scores_complete': '''SELECT COUNT(*) FROM news n
            WHERE n.status='connected' AND EXISTS (
                SELECT 1 FROM news_company_relations r WHERE r.news_id=n.news_id)
              AND NOT EXISTS (SELECT 1 FROM news_company_relations r
                  WHERE r.news_id=n.news_id AND r.status!='analysed')''',
        'news_extracted_or_created_with_relationships': '''SELECT COUNT(*) FROM news n
            WHERE n.status IN ('extracted','created') AND EXISTS (
                SELECT 1 FROM news_company_relations r WHERE r.news_id=n.news_id)''',
        'news_skipped_with_companies_or_relationships': '''SELECT COUNT(*) FROM news n
            WHERE n.status='skipped' AND (TRIM(n.companies)!='' OR EXISTS (
                SELECT 1 FROM news_company_relations r WHERE r.news_id=n.news_id))''',
        'news_scored_relations_on_skipped_articles': '''SELECT COUNT(*) FROM news_company_relations r
            JOIN news n ON n.news_id=r.news_id WHERE r.status='analysed' AND n.status='skipped' ''',
        'sec_analysed_items_on_unparsed_forms': '''SELECT COUNT(*) FROM items i
            JOIN forms f ON f.id=i.form_id WHERE i.status='analysed' AND f.status!='parsed' ''',
        'sec_analysed_items_without_explanations': '''SELECT COUNT(*) FROM items i
            WHERE i.status='analysed' AND (TRIM(i.ai_reason)='' OR
              TRIM(i.ai_reason)='Not analysed yet.' OR TRIM(i.ai_evidence)='')''',
    }
    anomalies = {name: db.execute(query).fetchone()[0] for name,query in sql.items()}
    queue = {
        'news_relations_ready_for_ai': db.execute('''SELECT COUNT(*) FROM news_company_relations r
            JOIN news n ON n.news_id=r.news_id WHERE r.status='connected' AND r.errors=''
              AND n.status IN ('connected','analysed','extracted')''').fetchone()[0],
        'sec_items_ready_for_ai': db.execute('''SELECT COUNT(*) FROM items i
            JOIN forms f ON f.id=i.form_id WHERE i.status='processed' AND f.status='parsed' ''').fetchone()[0],
        'sec_items_in_error_requiring_retry_flag': db.execute('''SELECT COUNT(*) FROM items i
            JOIN forms f ON f.id=i.form_id WHERE i.status='error' AND f.status='parsed' ''').fetchone()[0],
    }
    return dict(news_status=counts('news'),news_relation_status=counts('news_company_relations'),
                form_status=counts('forms'),sec_item_status=counts('items'),
                ai_queues=queue,status_anomalies=anomalies,
                status_anomaly_total=sum(anomalies.values()))


def main(argv=None):
    p=argparse.ArgumentParser(description='Read-only News and SEC AI status consistency audit')
    p.add_argument('--db',type=Path,help='SQLite path, default SEC_DB_PATH or Atlas data path')
    args=p.parse_args(argv)
    load_env()
    path=(args.db or database_path()).expanduser().resolve()
    if not path.is_file():
        p.error(f'Database not found: {path}')
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=30) as db:
            db.row_factory=sqlite3.Row
            result=audit(db)
        print(json.dumps(result,indent=2))
        return 2 if result['status_anomaly_total'] else 0
    except (sqlite3.Error,RuntimeError) as exc:
        p.exit(1, f'Status audit failed: {exc}\n')


if __name__=='__main__':
    raise SystemExit(main())
