#!/usr/bin/env python3
"""Atlas five-table SQLite cleanup (standard library only; SQLite 3.35+).

Build a *separate* clean, fully backed-up and checked SQLite database with
exactly: companies, raw_items, relationships, summary, price_bars.

  python3 atlas_five_table_migration.py --db /opt/atlas/data/edgar.sqlite3 --audit
  python3 atlas_five_table_migration.py --db /opt/atlas/data/edgar.sqlite3 --build
  python3 atlas_five_table_migration.py --db /opt/atlas/data/edgar.sqlite3 --verify --clean-db /opt/atlas/data/edgar.five-table.sqlite3

--build leaves the live database untouched. --activate is a separate guarded
cutover, supported ONLY after the main backend writers and dashboards have
been ported to the canonical schema and ALL database clients stopped.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

KEEP = ('companies', 'raw_items', 'relationships', 'summary', 'price_bars')
RETIRE = ('news_company_relations', 'items', 'forms', 'news')
REMOVE_REL_COLUMNS = ('legacy_relation_id', 'legacy_item_id')
KEEP_CANONICAL_TRIGGERS = frozenset({
    'atlas_unified_raw_status_guard_insert',
    'atlas_unified_raw_status_guard_update',
    'atlas_unified_raw_status_update',
    'atlas_unified_relationship_delete',
    'atlas_unified_relationship_insert',
    'atlas_unified_relationship_update',
    'atlas_mob_price_ai', 'atlas_mob_price_au', 'atlas_mob_price_ad',
})
PRODUCTION_FOLDERS = ('ai', 'news_pipeline', 'sec_edgar', 'dashboard', 'price_pipeline', 'shared')
LEGACY_SQL = re.compile(r'\b(?:FROM|JOIN|UPDATE|INTO|TABLE|REFERENCES|DELETE\s+FROM|ON)\s+' +
                        r'(?:IF\s+(?:NOT\s+)?EXISTS\s+)?["`\[]?' +
                        r'(?:news_company_relations|company_mobile_summary|news|forms|items)\b', re.I)


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def colnames(db, table):
    return {r[1] for r in db.execute('PRAGMA table_info(' + qi(table) + ')')}


def count(db, table):
    return db.execute('SELECT count(*) FROM ' + qi(table)).fetchone()[0]


def read_only(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=60)


def canonical(db):
    missing = set(KEEP) - tables(db)
    if missing:
        raise RuntimeError(f'Missing required tables: {sorted(missing)}. Apply the unified schema migration first.')
    unexpected = tables(db) - set(KEEP) - set(RETIRE)
    if unexpected:
        raise RuntimeError(f'Unexpected user tables: {sorted(unexpected)}. Refuse automatic data deletion.')
    required = {
        'raw_items': {'id', 'source_key', 'source_type', 'content', 'status', 'metadata_json'},
        'relationships': {'id', 'company_id', 'source_id', 'ai_impact', 'accumulated_impact', 'metadata_json'},
        'summary': {'company_id', 'last_price', 'trend_24h', 'trend_7d'},
        'price_bars': {'company_id', 'bar_time_utc', 'open', 'high', 'low', 'close', 'volume'},
        'companies': {'id', 'cik', 'name'},
    }
    for t, cols in required.items():
        missing_cols = cols - colnames(db, t)
        if missing_cols:
            raise RuntimeError(f'Missing columns in {t}: {sorted(missing_cols)}')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Foreign-key violations detected; no migration permitted')
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok':
        raise RuntimeError('SQLite integrity_check failed: ' + integrity)


def source_coverage(db):
    """Confirm every original content/relationship identity exists in unified tables."""
    legacy = tables(db)
    issues = []
    for old, kind, key, body in (('news', 'news', 'news_id', 'article_text'),
                                  ('forms', 'filing', 'id', None),
                                  ('items', 'item', 'id', 'item_text')):
        if old not in legacy:
            continue
        oldn = count(db, old)
        newn = db.execute('SELECT count(*) FROM raw_items WHERE source_type=?', (kind,)).fetchone()[0]
        if oldn != newn:
            issues.append(f'{old}: counts {oldn} != {newn}')
        missing = db.execute(f'''SELECT count(*) FROM {qi(old)} old
            LEFT JOIN raw_items src ON src.source_type=? AND src.source_key=CAST(old.{qi(key)} AS TEXT)
            WHERE src.id IS NULL''', (kind,)).fetchone()[0]
        if missing:
            issues.append(f'{old}: {missing} unmigrated source keys')
        if body:
            mismatch = db.execute(f'''SELECT count(*) FROM {qi(old)} old
                JOIN raw_items src ON src.source_type=? AND src.source_key=CAST(old.{qi(key)} AS TEXT)
                WHERE old.{qi(body)} IS NOT src.content''', (kind,)).fetchone()[0]
            if mismatch:
                issues.append(f'{old}: {mismatch} source text differences')
    if 'news_company_relations' in legacy:
        rcols = colnames(db, 'relationships')
        if 'legacy_relation_id' not in rcols:
            issues.append('News relation mapping column missing before source table retirement')
        else:
            missing = db.execute('''SELECT count(*) FROM news_company_relations old
                LEFT JOIN relationships r ON r.legacy_relation_id=old.relation_id
                WHERE r.id IS NULL''').fetchone()[0]
            if missing:
                issues.append(f'news_company_relations: {missing} unmigrated relations')
    if 'items' in legacy:
        if 'legacy_item_id' not in colnames(db, 'relationships'):
            issues.append('SEC item mapping column missing before source table retirement')
        else:
            missing = db.execute('''SELECT count(*) FROM items old
                LEFT JOIN relationships r ON r.legacy_item_id=old.id
                WHERE r.id IS NULL''').fetchone()[0]
            if missing:
                issues.append(f'items: {missing} unmigrated SEC assessment relations')
    if 'forms' in legacy:
        missing = db.execute('''SELECT count(*) FROM forms old
            JOIN raw_items src ON src.source_type='filing' AND src.source_key=CAST(old.id AS TEXT)
            LEFT JOIN relationships r ON r.source_id=src.id AND r.company_id=old.company_id
            WHERE r.id IS NULL''').fetchone()[0]
        if missing:
            issues.append(f'forms: {missing} filing/company associations not migrated')
    if issues:
        raise RuntimeError('Lossless migration checks failed:\n  ' + '\n  '.join(issues))


def scan_code(root):
    if not root.is_dir():
        raise RuntimeError(f'Source root missing: {root}')
    hits = []
    for folder in PRODUCTION_FOLDERS:
        if not (root / folder).is_dir():
            raise RuntimeError(f'Cannot prove backend compatibility: missing {root / folder}')
        for file in sorted((root / folder).rglob('*.py')):
            if LEGACY_SQL.search(file.read_text(encoding='utf-8', errors='replace')):
                hits.append(str(file.relative_to(root)))
    if (root / 'app.py').is_file() and LEGACY_SQL.search((root / 'app.py').read_text(encoding='utf-8', errors='replace')):
        hits.append('app.py')
    return hits


def db_report(db):
    canonical(db)
    print('RETAIN:', ', '.join(KEEP))
    for t in KEEP:
        print(f'  {t}: {count(db,t):,} rows; {len(colnames(db,t))} columns')
    print('RETIRE:', ', '.join(f'{t} ({count(db,t):,})' for t in RETIRE if t in tables(db)) or '(already removed)')
    cols = sorted(colnames(db, 'relationships') & set(REMOVE_REL_COLUMNS))
    print('DROP RELATIONSHIP COLUMNS:', ', '.join(cols) if cols else '(already removed)')
    print('SQLite internal sqlite_sequence is not a business table and may remain.')


def row_counts(db):
    return {t: count(db,t) for t in KEEP}


def clean_in_place_staged(db):
    """Mutate an ISOLATED snapshot only. All operations transactional."""
    canonical(db)
    source_coverage(db)
    before = row_counts(db)
    existing = tables(db)
    triggers = list(db.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"))
    views = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='view'"))
    if views:
        # Preserve user-created views only if they use surviving tables.
        bad = [name for name,sql in views if LEGACY_SQL.search(sql or '')]
        if bad:
            raise RuntimeError('Views depend on retiring tables: ' + ', '.join(bad))
    for name, owner, sql in triggers:
        if name in KEEP_CANONICAL_TRIGGERS:
            if LEGACY_SQL.search(sql or ''):
                raise RuntimeError(f'Canonical trigger {name} unexpectedly references legacy tables')
            continue
        if owner not in existing - set(RETIRE):
            # Triggers on retiring tables are removed with them; drop now for clarity.
            db.execute('DROP TRIGGER ' + qi(name))
        elif name.startswith('atlas_mob_') or name.startswith('atlas_unified_'):
            # Old summary/materialization trigger SQL queries the retired tables.
            db.execute('DROP TRIGGER ' + qi(name))
        elif LEGACY_SQL.search(sql or ''):
            raise RuntimeError('Third-party trigger depends on legacy tables: ' + name)
        # Unknown nonlegacy user trigger on kept tables remains intact.
    # Remove legacy identity indexes before pruning two redundant columns.
    db.execute('DROP INDEX IF EXISTS idx_rel_legacy_news')
    db.execute('DROP INDEX IF EXISTS idx_rel_legacy_item')
    relcols = colnames(db, 'relationships')
    if 'legacy_relation_id' in relcols or 'legacy_item_id' in relcols:
        invalid = db.execute('''SELECT count(*) FROM relationships
           WHERE NOT json_valid(metadata_json) AND
           (legacy_relation_id IS NOT NULL OR legacy_item_id IS NOT NULL)''').fetchone()[0]
        if invalid:
            raise RuntimeError(f'{invalid} relationship metadata JSON values invalid; cannot preserve legacy identities')
        if 'legacy_relation_id' in relcols:
            db.execute('''UPDATE relationships SET metadata_json=json_set(metadata_json,
               '$.archived_legacy_relation_id',legacy_relation_id)
               WHERE legacy_relation_id IS NOT NULL''')
        if 'legacy_item_id' in relcols:
            db.execute('''UPDATE relationships SET metadata_json=json_set(metadata_json,
               '$.archived_legacy_item_id',legacy_item_id)
               WHERE legacy_item_id IS NOT NULL''')
    # Foreign keys are disabled for the entire copy-cleanup transaction; rechecked afterward.
    for name in RETIRE:
        if name in existing:
            db.execute('DROP TABLE ' + qi(name))
    for name in REMOVE_REL_COLUMNS:
        if name in colnames(db,'relationships'):
            db.execute('ALTER TABLE relationships DROP COLUMN ' + qi(name))
    # In the simplified schema all status lives in raw_items, not relationships.
    extras = colnames(db, 'relationships') & {'status','analysed_at','accumulated_event_count','accumulated_news_count'}
    if extras:
        raise RuntimeError(f'Run simplified unified migration before cleanup; redundant columns: {sorted(extras)}')
    if not {'created','skipped','connected','analysed'} >= {
        x[0] for x in db.execute('SELECT DISTINCT status FROM raw_items')}:
        raise RuntimeError('Unexpected statuses present in raw_items')
    # Enforce that newly inserted company records always have a summary snapshot.
    db.execute('''CREATE TRIGGER IF NOT EXISTS atlas_canonical_company_summary_insert
       AFTER INSERT ON companies BEGIN
         INSERT OR IGNORE INTO summary(company_id,refreshed_at)
         VALUES (NEW.id,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
       END''')
    if tables(db) != set(KEEP):
        raise RuntimeError(f'Not exactly five tables: {sorted(tables(db))}')
    after = row_counts(db)
    if before != after:
        raise RuntimeError(f'Canonical row counts changed: {before} -> {after}')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Foreign keys invalid after table retirement')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('Integrity check failed after cleanup')
    left = [name for name,sql in db.execute('''SELECT name,sql FROM sqlite_master
        WHERE type IN ('trigger','view')''') if LEGACY_SQL.search(sql or '')]
    if left:
        raise RuntimeError('Remaining SQL references retired tables: ' + ', '.join(left))
    return before


def verify_clean(path, expected=None):
    with read_only(path) as db:
        canonical(db)
        if tables(db) != set(KEEP):
            raise RuntimeError(f'Expected exactly five business tables; found {sorted(tables(db))}')
        if colnames(db,'relationships') & set(REMOVE_REL_COLUMNS):
            raise RuntimeError('Legacy IDs still present as relationship columns')
        got = row_counts(db)
        if expected and got != expected:
            raise RuntimeError(f'Data count mismatch: {got} vs {expected}')
        references = [name for name,sql in db.execute('''SELECT name,sql FROM sqlite_master
          WHERE type IN ('view','trigger')''') if LEGACY_SQL.search(sql or '')]
        if references:
            raise RuntimeError(f'Legacy SQL references remain in clean DB: {references}')
    return got


def default_output(src):
    return src.with_name(src.stem + '.five-table.sqlite3')


def build(src, dst, vacuum=True):
    if src == dst:
        raise RuntimeError('Refuse to build over the live database. Use a distinct --clean-db path.')
    if dst.exists():
        raise RuntimeError(f'Target already exists: {dst}. Refuse to overwrite existing data.')
    for suffix in ('-wal','-shm'):
        if Path(str(dst)+suffix).exists():
            raise RuntimeError(f'Leftover SQLite sidecar at target: {dst}{suffix}; choose a fresh path')
    dst.parent.mkdir(parents=True, exist_ok=True)
    with read_only(src) as live:
        canonical(live)
        source_coverage(live)
        expected = row_counts(live)
        # Snapshot includes committed WAL data. Source is never altered.
        try:
            with sqlite3.connect(dst) as output:
                live.backup(output, pages=1024, sleep=0.05)
        except Exception:
            dst.unlink(missing_ok=True)
            raise
    try:
        with sqlite3.connect(dst, timeout=120, isolation_level=None) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('PRAGMA busy_timeout=120000')
            db.execute('BEGIN IMMEDIATE')
            try:
                clean_in_place_staged(db)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            if vacuum:
                # Physically reclaim pages formerly occupied by retired tables.
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('VACUUM')
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Foreign keys failed after compacting')
        actual = verify_clean(dst, expected)
        print('CLEAN DATABASE:', dst)
        print('COUNTS:', actual)
        print('VALIDATION: PASS. Live database unchanged.')
        print('IMPORTANT: Do not replace production until legacy SQL writers/readers are converted.')
    except BaseException:
        # A partial staging file should never be mistaken for a valid deliverable.
        broken = dst.with_name(dst.name+'.INCOMPLETE')
        if broken.exists():broken.unlink()
        if dst.exists():dst.rename(broken)
        raise
    return actual


def database_busy(path):
    """Require an external Linux check of DB/WAL/SHM file descriptors."""
    fuser = shutil.which('fuser')
    if not fuser:
        raise RuntimeError('Cannot verify offline database: install psmisc/fuser before --activate')
    paths = [str(p) for p in (path, Path(str(path)+'-wal'), Path(str(path)+'-shm')) if p.exists()]
    result = subprocess.run([fuser, '-s', *paths], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode == 0:
        raise RuntimeError('Database is still open by a process; stop all database clients and retry')
    if result.returncode != 1:
        raise RuntimeError(f'Cannot check live database users (fuser code {result.returncode})')


def activate(path, root, no_vacuum=False):
    """Offline cutover only after source tree and running processes are clean."""
    hits = scan_code(root)
    if hits:
        raise RuntimeError('CUTOVER BLOCKED — legacy SQL still present in production modules:\n  '+ '\n  '.join(hits))
    database_busy(path)
    with read_only(path) as db:
        canonical(db)
        source_coverage(db)
    # Freeze is operator-enforced; fuser detects existing file descriptors.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stage = path.with_name(path.name+'.five-table-stage-'+stamp)
    with sqlite3.connect(path, timeout=60) as db:
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and checkpoint[0]:
            raise RuntimeError(f'Live database WAL checkpoint blocked: {checkpoint}')
    database_busy(path)
    baseline = (path.stat().st_size, path.stat().st_mtime_ns)
    print('Building final clean snapshot with all writers stopped...')
    build(path, stage, vacuum=not no_vacuum)
    if baseline != (path.stat().st_size, path.stat().st_mtime_ns):
        raise RuntimeError('Live database changed while clean snapshot was built; no cutover performed')
    database_busy(path)
    with sqlite3.connect(path) as db:
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and checkpoint[0]:
            raise RuntimeError(f'Live WAL checkpoint blocked: {checkpoint}')
    database_busy(path)
    if baseline != (path.stat().st_size, path.stat().st_mtime_ns):
        # WAL checkpoint can legitimately touch mtime; a changed main DB file
        # requires rebuilding a fresh consistent snapshot, not guessing.
        raise RuntimeError('Source database changed during cutover checks; no replacement performed')
    original = path.with_name(path.name+'.before-five-table-'+stamp+'.sqlite3')
    # Atomic two-rename cutover on same filesystem. The original is retained,
    # including any legacy tables, as a complete rollback file.
    sidecars = [Path(str(path)+s) for s in ('-wal','-shm')]
    if any(p.exists() and p.stat().st_size > 0 for p in sidecars):
        raise RuntimeError('Nonempty WAL/SHM sidecars found. No cutover performed')
    os.replace(path, original)
    try:
        os.replace(stage, path)
    except BaseException:
        os.replace(original, path)
        raise
    for p in sidecars:
        p.unlink(missing_ok=True)
    try:
        print('CUTOVER COMPLETE. Verified final database:', verify_clean(path))
        print('ROLLBACK ORIGINAL:', original)
        print('Do NOT start legacy SQL services; deploy and verify canonical writers first.')
    except BaseException:
        # Leave both artifacts for inspection; do not auto-restore while clients
        # might have begun opening the new path.
        print('WARNING: Post-cutover validation failed; STOP services and restore', original, file=sys.stderr)
        raise


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True,type=Path)
    p.add_argument('--clean-db',type=Path)
    p.add_argument('--source-root',type=Path,default=Path('/opt/atlas/main_back_end'))
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--audit',action='store_true')
    mode.add_argument('--build',action='store_true')
    mode.add_argument('--verify',action='store_true')
    mode.add_argument('--can-cutover',action='store_true')
    mode.add_argument('--activate',action='store_true',help='Offline guarded cutover (DANGEROUS unless all clients converted and stopped)')
    p.add_argument('--confirm-cutover',action='store_true',help='Acknowledge all backend clients are stopped/converted')
    p.add_argument('--no-vacuum',action='store_true',help='Skip VACUUM on staged DB (faster, may retain free pages)')
    a=p.parse_args(argv)
    if sqlite3.sqlite_version_info < (3, 35):
        raise RuntimeError('Python SQLite version 3.35 or later is required for DROP COLUMN')
    source=a.db.expanduser().resolve(strict=True)
    clean=(a.clean_db or default_output(source)).expanduser().resolve()
    if a.verify:
        print('VALIDATED FIVE-TABLE COPY:', clean, verify_clean(clean))
        return
    if a.activate:
        if not a.confirm_cutover:
            raise RuntimeError('Use --confirm-cutover only after converting ALL main backend and Dash consumers and stopping ALL database clients')
        activate(source, a.source_root, no_vacuum=a.no_vacuum)
        return
    with read_only(source) as db:
        db_report(db)
        if a.audit:
            source_coverage(db)
            print('Data coverage: PASS')
            try:
                hits=scan_code(a.source_root)
                print(f'Main backend legacy SQL modules: {len(hits)}')
                for hit in hits:print('  '+hit)
                if hits:print('CUTOVER BLOCKED until main backend is converted; building a copy remains safe.')
            except RuntimeError as e:
                print('Source code compatibility not proven:',e)
            return
    if a.can_cutover:
        hits=scan_code(a.source_root)
        if hits:
            raise RuntimeError('CUTOVER BLOCKED by legacy SQL in: '+', '.join(hits))
        verify_clean(clean)
        print('Source scan passed. Confirm ALL collectors, Dash, cron, and external readers are converted and stopped before manual cutover.')
        return
    build(source,clean,vacuum=not a.no_vacuum)


if __name__=='__main__':
    try:main()
    except (RuntimeError, ValueError, sqlite3.Error, OSError) as exc:
        print('FAILED:', exc, file=sys.stderr)
        raise SystemExit(1)
