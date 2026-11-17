#!/usr/bin/env python3
"""Data-preserving Atlas schema simplification (run after unified migration).

Stops at one raw_items.status, four states, and two accumulated values. Keeps
legacy stage tables until all collectors and dashboards use canonical writes.

  python3 scripts/migrate_simplified_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --dry-run
  python3 scripts/migrate_simplified_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --apply
  python3 scripts/migrate_simplified_unified_model.py --db /opt/atlas/data/edgar.sqlite3 --verify
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import legacy_unified_model as unified_model

REMOVE_RAW = ('parent_source','analysis_scope')
REMOVE_REL = ('status','analysed_at','accumulated_event_count','accumulated_news_count')
STATES = {'created','skipped','connected','analysed'}


def columns(db,table):
    return set(unified_model.cols(db,table))


def verify(db):
    raw=columns(db,'raw_items');rel=columns(db,'relationships')
    if 'status' not in raw or 'processing_status' in raw or any(x in raw for x in REMOVE_RAW):
        raise RuntimeError('raw_items has not been simplified')
    if any(x in rel for x in REMOVE_REL):
        raise RuntimeError('relationships still has redundant workflow/count columns')
    bad=db.execute("SELECT status,COUNT(*) FROM raw_items WHERE status NOT IN ('created','skipped','connected','analysed') GROUP BY status").fetchall()
    if bad:raise RuntimeError(f'Invalid raw_items statuses: {bad}')
    wrong=db.execute("SELECT COUNT(*) FROM raw_items WHERE source_type='filing' AND source_name!='SEC'").fetchone()[0]
    if wrong:raise RuntimeError(f'{wrong} filings are not labeled SEC')
    wrong=db.execute("""SELECT COUNT(*) FROM raw_items WHERE source_type='item' AND
       COALESCE(source_name,'')<>COALESCE((SELECT json_extract(f.metadata_json,'$.form_type')
         FROM raw_items f WHERE f.source_type='filing' AND f.source_key=CAST(json_extract(raw_items.metadata_json,'$.form_id') AS TEXT)),'')
       AND json_extract(metadata_json,'$.form_id') IS NOT NULL""").fetchone()[0]
    if wrong:raise RuntimeError(f'{wrong} SEC items lack the matching form source_name')
    fk=db.execute('PRAGMA foreign_key_check').fetchall()
    if fk:raise RuntimeError(f'Foreign-key errors {fk[:4]}')
    integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity!='ok':raise RuntimeError('SQLite integrity: '+integrity)
    print('Schema/status/source-name verification: PASS')


def migrate(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'raw_items','relationships','summary','companies'}.issubset(tables):
        raise RuntimeError('Unified migration must complete first (run migrate_unified_model.py)')
    old_raw=columns(db,'raw_items');old_rel=columns(db,'relationships')
    before=(db.execute('SELECT COUNT(*) FROM raw_items').fetchone()[0],
            db.execute('SELECT COUNT(*) FROM relationships').fetchone()[0])
    if 'processing_status' not in old_raw and 'status' in old_raw and not (set(REMOVE_RAW)&old_raw or set(REMOVE_REL)&old_rel):
        verify(db);print('Already migrated: no changes');return
    if 'processing_status' not in old_raw:
        raise RuntimeError('Unexpected partial schema upgrade; restore/check backup before retrying')
    # Remove canonical compatibility triggers before changing their referenced columns.
    triggers=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_unified_%'")]
    for name in triggers:db.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
    # A DB view/third-party trigger may use removed columns; fail transactionally.
    dependencies=db.execute("""SELECT type,name FROM sqlite_master WHERE type IN ('trigger','view') AND
       (instr(lower(coalesce(sql,'')),'raw_items.processing_status')>0 OR
        instr(lower(coalesce(sql,'')),'raw_items.parent_source')>0 OR
        instr(lower(coalesce(sql,'')),'relationships.status')>0)
       """).fetchall()
    if dependencies:
        raise RuntimeError(f'Other dependent views/triggers must be upgraded first: {dependencies}')
    db.execute('DROP INDEX IF EXISTS idx_rel_company_status')
    # The previous model normalized collection statuses into unrelated values.
    # Restore canonical workflow status from source records before removing it.
    db.execute("ALTER TABLE raw_items RENAME COLUMN processing_status TO status")
    for col in REMOVE_RAW:
        if col in old_raw:db.execute('ALTER TABLE raw_items DROP COLUMN '+col)
    for col in REMOVE_REL:
        if col in old_rel:db.execute('ALTER TABLE relationships DROP COLUMN '+col)
    db.execute("""UPDATE raw_items SET status=CASE
      WHEN source_type='news' THEN COALESCE((SELECT CASE
        WHEN n.status='analysed' THEN 'analysed'
        WHEN n.status IN ('skipped','rejected','filtered') THEN 'skipped'
        WHEN n.status='connected' THEN 'connected' ELSE 'created' END
        FROM news n WHERE n.news_id=raw_items.source_key),
        CASE WHEN status='deleted' THEN 'skipped' ELSE 'created' END)
      WHEN source_type='item' THEN COALESCE((SELECT CASE
        WHEN i.status='analysed' THEN 'analysed'
        WHEN i.status IN ('skipped','rejected','filtered') THEN 'skipped'
        WHEN i.status='connected' THEN 'connected' ELSE 'created' END
        FROM items i WHERE CAST(i.id AS TEXT)=raw_items.source_key),
        CASE WHEN status='deleted' THEN 'skipped' ELSE 'created' END)
      WHEN source_type='filing' THEN COALESCE((SELECT CASE
        WHEN f.status='analysed' THEN 'analysed'
        WHEN f.status IN ('skipped','rejected','filtered') THEN 'skipped'
        WHEN f.status='connected' THEN 'connected' ELSE 'created' END
        FROM forms f WHERE CAST(f.id AS TEXT)=raw_items.source_key),
        CASE WHEN status='deleted' THEN 'skipped' ELSE 'created' END)
      ELSE 'created' END""")
    db.execute("""UPDATE raw_items SET status=CASE
      WHEN EXISTS (SELECT 1 FROM news_company_relations l WHERE l.news_id=raw_items.source_key
        AND l.entity_type='company' AND l.status NOT IN ('analysed','skipped','rejected')) THEN 'connected'
      WHEN EXISTS (SELECT 1 FROM news_company_relations l WHERE l.news_id=raw_items.source_key
        AND l.entity_type='company' AND l.status='analysed') THEN 'analysed'
      WHEN EXISTS (SELECT 1 FROM news_company_relations l WHERE l.news_id=raw_items.source_key
        AND l.entity_type='company') THEN 'skipped' ELSE status END WHERE source_type='news'""")
    db.execute("UPDATE raw_items SET status='connected' WHERE source_type='filing' AND status='created'")
    db.execute("UPDATE raw_items SET source_name='SEC' WHERE source_type='filing'")
    db.execute("""UPDATE raw_items SET source_name=COALESCE((SELECT json_extract(f.metadata_json,'$.form_type')
      FROM raw_items f WHERE f.source_type='filing' AND f.source_key=CAST(json_extract(raw_items.metadata_json,'$.form_id') AS TEXT)),
      NULLIF(source_name,'SEC'), 'SEC') WHERE source_type='item'""")
    # JSON metadata retains source-specific properties, not shadow copies of
    # workflow state or deleted cumulative-count columns.
    db.execute('''UPDATE raw_items SET metadata_json=json_remove(metadata_json,'$.status')
       WHERE json_valid(metadata_json)''')
    db.execute('''UPDATE relationships SET metadata_json=json_remove(metadata_json,
       '$.status','$.analysed_at','$.accumulated_event_count','$.accumulated_news_count')
       WHERE json_valid(metadata_json)''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_rel_company ON relationships(company_id)')
    # Preserve already saved AI metrics and cumulative values, recalculating when older
    # status changes previously excluded an event. No fabricated impact is inserted.
    for query in unified_model.recalculate_sql().split(';'):
        if query.strip():db.execute(query)
    unified_model.install_mirror_triggers(db)
    after=(db.execute('SELECT COUNT(*) FROM raw_items').fetchone()[0],
           db.execute('SELECT COUNT(*) FROM relationships').fetchone()[0])
    if before!=after:raise RuntimeError(f'Unexpected row count difference: {before} -> {after}')
    verify(db)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    group=p.add_mutually_exclusive_group(required=True)
    for mode in ('dry-run','apply','verify'):group.add_argument('--'+mode,action='store_true')
    a=p.parse_args(argv);path=a.db.resolve(strict=True)
    if sqlite3.sqlite_version_info<(3,35,0):
        raise RuntimeError('SQLite 3.35+ is required for DROP COLUMN; upgrade Python sqlite3 before applying')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA busy_timeout=30000')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Existing database integrity check failed')
        print('Database:',path)
        for table in ('raw_items','relationships','summary'):
            print(table,db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        if a.verify:verify(db);return
        if a.dry_run:
            print('DRY RUN — read-only; backup and schema changes require --apply');return
    # SQLite online backup API also captures committed WAL changes safely.
    backup=path.with_name(path.name+'.before-schema-simplification-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.bak')
    with sqlite3.connect(path,timeout=60) as src,sqlite3.connect(backup) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup integrity check failed')
    print('BACKUP:',backup)
    with sqlite3.connect(path,timeout=60,isolation_level=None) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=60000')
        db.execute('BEGIN IMMEDIATE')
        try:
            migrate(db)
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    print('COMPLETE. Legacy staging tables were not removed (active ingestion depends on them).')

if __name__=='__main__':
    try:main()
    except (sqlite3.Error,RuntimeError,OSError,ValueError) as e:
        print('FAILED:',e,file=sys.stderr);sys.exit(1)
