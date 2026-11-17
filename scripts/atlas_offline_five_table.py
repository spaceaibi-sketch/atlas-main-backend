#!/usr/bin/env python3
"""Offline, backed-up Atlas five-table cutover for the canonical-only backend.

The production database is NOT changed by `stage`. It creates a full
pre-migration SQLite backup, reconciles historical source/link rows (including
any content conflicts) into the five retained tables on an offline copy,
and verifies the copy. `activate` separately swaps the tested five-table copy
into place after stopping all Atlas services, jobs and SQLite writers.

Commands:
 python3 main_back_end/scripts/atlas_offline_five_table.py stage \
   --db /opt/atlas/data/edgar.sqlite3 \
   --out-dir /opt/atlas_build/database \
   --source-root /opt/atlas/main_back_end
 python3 main_back_end/scripts/atlas_offline_five_table.py activate \
   --db /opt/atlas/data/edgar.sqlite3 \
   --stage-file <path-from-stage-output> \
   --report <path-from-stage-output> \
   --acknowledge-verified-five-table-cutover

This tool does NOT validate production-sized data here. A full original backup
is retained, while full legacy record snapshots and relationship histories
remain available under the canonical metadata_json fields after activation.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

KEEP = {'companies','raw_items','relationships','summary','price_bars'}
RETIRE = {'news','forms','items','news_company_relations'}
REL_COLS = {'legacy_item_id', 'legacy_relation_id'}
LEGACY_SQL = re.compile(r'\b(?:FROM|JOIN|UPDATE|INTO|TABLE|REFERENCES|DELETE\s+FROM|ON)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?["`\[]?(?:news_company_relations|news|forms|items)\b', re.I)
PRODUCTION_FOLDERS = ('ai', 'news_pipeline', 'sec_edgar','dashboard','price_pipeline','shared')


def quote(s):
    return '"' + s.replace('"','""') + '"'


def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def cols(con, t):
    return {r[1] for r in con.execute('PRAGMA table_info(' + quote(t) + ')')}


def counts(con, ts):
    return {t: con.execute('SELECT count(*) FROM ' + quote(t)).fetchone()[0] for t in sorted(ts)}


def verify_integrity(con):
    res = con.execute('PRAGMA integrity_check').fetchone()[0]
    if res != 'ok':
        raise RuntimeError('SQLite integrity_check failed: ' + str(res))
    viol = con.execute('PRAGMA foreign_key_check').fetchall()
    if viol:
        raise RuntimeError(f'{len(viol)} foreign-key violations detected')


@contextmanager
def ro(path):
    con = sqlite3.connect(path.as_uri() + '?mode=ro',uri=True,timeout=120)
    try:
        yield con
    finally:
        con.close()


def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def source_scan(root):
    hits=[]
    for d in PRODUCTION_FOLDERS:
        path=root/d
        if not path.is_dir():
            hits.append(f'MISSING:{path}')
            continue
        for f in path.rglob('*.py'):
            if LEGACY_SQL.search(f.read_text('utf8',errors='replace')):
                hits.append(str(f.relative_to(root)))
    app = root / 'app.py'
    if app.is_file() and LEGACY_SQL.search(app.read_text('utf8',errors='replace')):
        hits.append('app.py')
    return sorted(hits)


def unused(path):
    if path.exists() or Path(str(path)+'-wal').exists() or Path(str(path)+'-shm').exists():
        raise RuntimeError(f'Output destination already exists: {path}')


def busy(path):
    utility=shutil.which('fuser')
    if utility is None:
        raise RuntimeError('Install psmisc (fuser) to check live database locks')
    p=[str(x) for x in (path,Path(str(path)+'-wal'),Path(str(path)+'-shm')) if x.exists()]
    result=subprocess.run([utility,'-s',*p],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if result.returncode == 0:
        raise RuntimeError('A process has the SQLite DB open. Stop Java, Dash, collectors and cron before continuing')
    if result.returncode != 1:
        raise RuntimeError(f'fuser check failed with code {result.returncode}')


def legacy_coverage(con):
    """Numbers reveal that data archival is NOT equal to canonical migration."""
    present=tables(con)
    issues={}
    for legacy,source_type,identity,body in [('news','news','news_id','article_text'),('forms','filing','id',None),('items','item','id','item_text')]:
        if legacy not in present:continue
        issues[legacy] = {'legacy_rows':counts(con,[legacy])[legacy]}
        issues[legacy]['keys_not_in_raw_items'] = con.execute(
            f'SELECT count(*) FROM {quote(legacy)} old LEFT JOIN raw_items r ON r.source_type=? AND r.source_key=CAST(old.{quote(identity)} AS TEXT) WHERE r.id IS NULL',
            (source_type,)).fetchone()[0]
        if body:
            issues[legacy]['content_differences'] = con.execute(
                f'SELECT count(*) FROM {quote(legacy)} old JOIN raw_items r ON r.source_type=? AND r.source_key=CAST(old.{quote(identity)} AS TEXT) WHERE old.{quote(body)} IS NOT r.content',
                (source_type,)).fetchone()[0]
    if 'news_company_relations' in present:
        issues['news_company_relations']={'legacy_rows':counts(con,['news_company_relations'])['news_company_relations']}
        if 'legacy_relation_id' in cols(con,'relationships'):
            issues['news_company_relations']['unmapped_relations']=con.execute('''SELECT count(*) FROM news_company_relations old LEFT JOIN relationships r ON r.legacy_relation_id=old.relation_id WHERE r.id IS NULL''').fetchone()[0]
    return issues


def _archive_row(con, *, table, source_type, old_key, source_fields, snapshot,
                 parent_key=None, metadata=None):
    """Merge one historical source without overwriting canonical content/AI.

    Historical identifiers are heterogeneous (UUID, accession, numeric SEC ID);
    the canonical unique source_type/source_key key resolves each source safely.
    Older conflicting source content is retained verbatim in JSON, not discarded.
    """
    payload=dict(snapshot)
    meta=dict(metadata or {})
    meta.update(payload)
    meta['_archived_legacy']=payload
    now=payload.get('updated_at') or payload.get('created_at')
    source_key=str(old_key)
    current=con.execute('''SELECT id,metadata_json FROM raw_items WHERE source_type=?
       AND source_key=?''',(source_type,source_key)).fetchone()
    if current:
        if not current[1] or not json.loads(current[1]):
            merged={}
        else:
            merged=json.loads(current[1])
        merged['_archived_legacy']=payload
        if 'stage' in meta and 'stage' not in merged:
            merged['stage']=meta['stage']
        con.execute('UPDATE raw_items SET metadata_json=? WHERE id=?',
                    (json.dumps(merged,ensure_ascii=False,sort_keys=True),current[0]))
        return current[0],False
    fields='source_type,source_key,parent_key,title,content,summary,event_at,received_at,source_url,source_name,status,metadata_json,created_at,updated_at'
    values=(source_type,source_key,parent_key,
            source_fields.get('title') or '',source_fields.get('content') or '',
            source_fields.get('summary'),source_fields.get('event_at'),
            source_fields.get('received_at'),source_fields.get('source_url'),
            source_fields.get('source_name'),source_fields.get('status','created'),
            json.dumps(meta,ensure_ascii=False,sort_keys=True),
            payload.get('created_at') or now,payload.get('updated_at') or now)
    cur=con.execute('INSERT INTO raw_items ('+fields+') VALUES ('+','.join('?'*len(values))+')',values)
    return cur.lastrowid,True


def _archive_relation(con, snapshot, source_id, *, company_id, entity_type,
                      entity_key, entity_name, assessed=False):
    """One historical relationship maps to one canonical identity or its archive."""
    found=con.execute('SELECT id,metadata_json FROM relationships WHERE source_id=? AND entity_key=?',
                      (source_id,entity_key)).fetchone()
    if found:
        payload=json.loads(found[1] or '{}')
        history=payload.get('_archived_legacy_relations',[])
        history.append(snapshot)
        payload['_archived_legacy_relations']=history
        con.execute('UPDATE relationships SET metadata_json=? WHERE id=?',
                    (json.dumps(payload,ensure_ascii=False,sort_keys=True),found[0]))
        return found[0],False
    base={'company_id':company_id,'source_id':source_id,'entity_type':entity_type,
          'entity_key':entity_key,'entity_name':entity_name or '',
          'metadata_json':json.dumps({'_archived_legacy_relations':[snapshot]},
             ensure_ascii=False,sort_keys=True),
          'created_at':snapshot.get('created_at'),'updated_at':snapshot.get('updated_at')}
    if assessed:
        for col in ('ai_impact','ai_confidence','ai_reason','ai_evidence',
                    'ai_monetary_effect','ai_monetary_effect_reason',
                    'price_change_1w_pct','price_change_reason'):
            base[col]=snapshot.get(col)
    for col in ('correlation_reason','match_evidence','match_hits'):
        if col in snapshot:base[col]=snapshot[col]
    columns=','.join('"'+name+'"' for name in base)
    cur=con.execute('INSERT INTO relationships ('+columns+') VALUES ('+
                 ','.join('?'*len(base))+')',tuple(base.values()))
    return cur.lastrowid,True


def reconcile_legacy(con):
    """Archive all unmatched legacy data into the five canonical tables.

    Identity matching is exact. Already-mapped canonical values are NEVER
    overwritten; conflicting old values stay in metadata._archived_legacy.
    """
    present=tables(con)
    changed={'news':0,'forms':0,'items':0,'news_company_relations':0,
             'filing_associations':0,'item_associations':0}
    news_map={}; filing_map={}; item_map={}
    if 'news' in present:
        for old in con.execute('SELECT * FROM news'):
            r=dict(old); k=r['news_id']
            status=r.get('status')
            status=status if status in ('created','skipped','connected','analysed') else 'created'
            source={'title':r.get('title'),'content':r.get('article_text'),
                    'summary':r.get('summary'),'event_at':r.get('published_utc'),
                    'received_at':r.get('created_at'),'source_url':r.get('url'),
                    'source_name':r.get('source'),'status':status}
            sid,added=_archive_row(con,table='news',source_type='news',old_key=k,
                                   source_fields=source,snapshot=r,
                                   metadata={'stage':r.get('status') if r.get('status')=='extracted' else status})
            news_map[k]=sid;changed['news']+=int(added)
    if 'forms' in present:
        for old in con.execute('SELECT * FROM forms'):
            r=dict(old); k=r['id'];phase=r.get('status')
            source={'title':r.get('form_name'),'event_at':r.get('filed_date'),
                    'received_at':r.get('created_at'),'source_url':r.get('index_url'),
                    'source_name':'SEC','status':'connected' if phase=='parsed' else 'created'}
            sid,added=_archive_row(con,table='forms',source_type='filing',old_key=k,
                                   source_fields=source,snapshot=r,
                                   metadata={'stage':phase})
            filing_map[k]=sid;changed['forms']+=int(added)
            cid=r.get('company_id')
            if cid is not None:
                name=con.execute('SELECT name FROM companies WHERE id=?',(cid,)).fetchone()
                _id,inserted=_archive_relation(con,r,sid,company_id=cid,
                   entity_type='company',entity_key='company:'+str(cid),
                   entity_name=name[0] if name else '')
                changed['filing_associations']+=int(inserted)
    if 'items' in present:
        for old in con.execute('SELECT * FROM items'):
            r=dict(old);k=r['id'];fid=r.get('form_id')
            form=con.execute('SELECT company_id,index_url,filed_date,form_type FROM forms WHERE id=?',
                             (fid,)).fetchone() if 'forms' in present else None
            source={'title':r.get('item_name'),'content':r.get('item_text'),
                    'event_at':form['filed_date'] if form else None,
                    'received_at':r.get('created_at'),
                    'source_url':form['index_url'] if form else None,
                    'source_name':form['form_type'] if form else 'SEC',
                    'status':'analysed' if r.get('status')=='analysed' else 'created'}
            sid,added=_archive_row(con,table='items',source_type='item',old_key=k,
               source_fields=source,snapshot=r,
               parent_key=str(fid) if fid is not None else None,
               metadata={'stage':r.get('status')})
            item_map[k]=sid;changed['items']+=int(added)
            cid=form['company_id'] if form else None
            if cid is not None:
                name=con.execute('SELECT name FROM companies WHERE id=?',(cid,)).fetchone()
                _id,inserted=_archive_relation(con,r,sid,company_id=cid,
                   entity_type='company',entity_key='company:'+str(cid),
                   entity_name=name[0] if name else '',
                   assessed=r.get('status')=='analysed')
                changed['item_associations']+=int(inserted)
    if 'news_company_relations' in present:
        for old in con.execute('SELECT * FROM news_company_relations'):
            r=dict(old);source_id=news_map.get(r['news_id'])
            if source_id is None:
                raise RuntimeError('Historical news relationship missing source: '+str(r['news_id']))
            _,added=_archive_relation(con,r,source_id,
                  company_id=r.get('company_id'),entity_type=r.get('entity_type') or 'company',
                  entity_key=r['entity_key'],entity_name=r.get('entity_name'),
                  assessed=r.get('status')=='analysed')
            changed['news_company_relations']+=int(added)
    # Presence of the old row snapshot in the target is an exact record-level
    # completeness proof even when newer canonical content differs.
    for table,kind,key,known in (
        ('news','news','news_id',news_map),('forms','filing','id',filing_map),
        ('items','item','id',item_map)):
        if table not in present: continue
        for old in con.execute('SELECT * FROM "'+table+'"'):
            snap=dict(old);sid=known[snap[key]]
            record=con.execute('SELECT metadata_json FROM raw_items WHERE id=? AND source_type=?',
                               (sid,kind)).fetchone()
            if not record or json.loads(record[0]).get('_archived_legacy')!=snap:
                raise RuntimeError('Historical source archive mismatch: '+table+' '+str(snap[key]))
    # Check every historical news relation by relation_id within archival metadata.
    if 'news_company_relations' in present:
        for old in con.execute('SELECT * FROM news_company_relations'):
            snap=dict(old);sid=news_map[snap['news_id']]
            found=con.execute('SELECT metadata_json FROM relationships WHERE source_id=? AND entity_key=?',
                              (sid,snap['entity_key'])).fetchone()
            if not found or snap not in json.loads(found[0]).get('_archived_legacy_relations',[]):
                raise RuntimeError('News linkage archive mismatch: '+str(snap['relation_id']))
    return changed


def staged_cleanup(con):
    present=tables(con)
    if not KEEP <= present:
        raise RuntimeError('Missing canonical tables: '+', '.join(sorted(KEEP-present)))
    if present - KEEP - RETIRE:
        raise RuntimeError('Unrecognized tables cannot be deleted: '+', '.join(sorted(present-KEEP-RETIRE)))
    verify_integrity(con)
    original=counts(con,KEEP)
    con.execute('PRAGMA foreign_keys=OFF')
    con.execute('BEGIN IMMEDIATE')
    try:
        # Legacy views/triggers cannot be retained with the retired tables.
        for kind,name,tbl,sql in con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('view','trigger')").fetchall():
            if kind=='view' and LEGACY_SQL.search(sql or ''):
                con.execute('DROP VIEW '+quote(name))
            elif kind=='trigger' and (tbl in RETIRE or LEGACY_SQL.search(sql or '')):
                con.execute('DROP TRIGGER '+quote(name))
        con.execute('DROP INDEX IF EXISTS idx_rel_legacy_news')
        con.execute('DROP INDEX IF EXISTS idx_rel_legacy_item')
        for col in REL_COLS & cols(con,'relationships'):
            missing=con.execute(f'''SELECT count(*) FROM relationships WHERE {quote(col)} IS NOT NULL AND NOT json_valid(COALESCE(metadata_json, '{{}}'))''').fetchone()[0]
            if missing:
                raise RuntimeError(f'Invalid relationship metadata: cannot archive {col} of {missing} rows')
            con.execute(f'''UPDATE relationships SET metadata_json=json_set(COALESCE(metadata_json, '{{}}'), '$.archived_{col}', {quote(col)}) WHERE {quote(col)} IS NOT NULL''')
        for table in sorted(RETIRE&present):
            con.execute('DROP TABLE '+quote(table))
        for col in sorted(REL_COLS & cols(con,'relationships')):
            con.execute('ALTER TABLE relationships DROP COLUMN '+quote(col))
        if tables(con) != KEEP or cols(con,'relationships') & REL_COLS:
            raise RuntimeError('Not exactly five canonical business tables after cleanup')
        if counts(con, KEEP) != original:
            raise RuntimeError('Canonical row counts changed during cleanup')
        con.execute('COMMIT')
    except BaseException:
        con.execute('ROLLBACK')
        raise
    verify_integrity(con)
    return original


def stage(db,out,source_root):
    db=db.resolve(strict=True)
    busy(db)
    if any(x.exists() and x.stat().st_size > 0 for x in (Path(str(db)+'-wal'),Path(str(db)+'-shm'))):
        raise RuntimeError('Nonempty WAL/SHM detected. Stop writers, checkpoint and close connections before staging')
    source_checksum=sha256(db)
    out=out.expanduser().resolve()
    out.mkdir(parents=True,exist_ok=True)
    if not out.is_dir():raise RuntimeError('Stage path not a directory')
    source_size=db.stat().st_size
    required=int(source_size*2.5) + 50*1024*1024
    if shutil.disk_usage(out).free < required:
        raise RuntimeError(f'Not enough free space under {out}; require at least {required:,} bytes to stage archive and copy')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    archive=out/f'edgar.original.{stamp}.sqlite3'
    staged=out/f'edgar.five-tables.{stamp}.stage.sqlite3'
    report=out/f'edgar.five-tables.{stamp}.json'
    for p in (archive,staged,report):unused(p)
    try:
        with ro(db) as original:
            if not KEEP <= tables(original):raise RuntimeError('Original database lacks canonical tables')
            verify_integrity(original)
            old_counts=counts(original,tables(original))
            gaps=legacy_coverage(original)
            with sqlite3.connect(archive) as dest:original.backup(dest,pages=256,sleep=0.1)
        archive.chmod(0o600)
        with ro(archive) as src, sqlite3.connect(staged) as dest:
            src.backup(dest,pages=256,sleep=0.1)
        with sqlite3.connect(staged,isolation_level=None,timeout=120) as c:
            c.row_factory=sqlite3.Row
            c.execute('BEGIN IMMEDIATE')
            try:
                reconciliation=reconcile_legacy(c)
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
            clean_counts=staged_cleanup(c)
            c.execute('PRAGMA journal_mode=DELETE')
            c.execute('VACUUM')
        with ro(staged) as c:
            verify_integrity(c)
            if tables(c)!=KEEP:raise RuntimeError('Incorrect stage tables')
            if counts(c,KEEP)!=clean_counts:raise RuntimeError('Count changed after VACUUM')
        with ro(archive) as src:
            verify_integrity(src)
            if counts(src,old_counts)!=old_counts:raise RuntimeError('Original archive changed')
        staged.chmod(0o600)
        blockers=source_scan(source_root)
        obj={
            'format':'Atlas five-table canonical runtime archival cutover, version 2',
            'source_db':str(db),'archive_db':str(archive),'stage_db':str(staged),
            'original_counts':old_counts,'retained_counts':clean_counts,'source_sha256':source_checksum,
            'original_legacy_coverage_gaps':gaps,'archival_reconciliation':reconciliation,
            'python_modules_depending_on_legacy_tables':blockers,
            'archive_sha256':sha256(archive),'stage_sha256':sha256(staged),
            'warning':'Each legacy row is saved verbatim in canonical metadata. Existing canonical text and scores are retained without replacement. Only activate after validating the new runtime and this staged database.',
        }
        report.write_text(json.dumps(obj,indent=2)+'\n');report.chmod(0o600)
        print('COMPLETE ARCHIVE:',archive)
        print('FIVE-TABLE STAGE:',staged)
        print('READINESS REPORT:',report)
        print('RETAINED ROWS:',clean_counts)
        print('LEGACY CODE DEPENDENCIES:',len(blockers))
        print('All historical source snapshots preserved in canonical metadata:', reconciliation)
        print('Production database NOT CHANGED.')
    except BaseException:
        # Do not delete a good archive if a stage step fails: operator can recover it.
        if archive.exists():print('ARCHIVE (preserved after error):',archive,file=sys.stderr)
        if staged.exists():
            incomplete=Path(str(staged)+'.INCOMPLETE')
            staged.rename(incomplete)
        raise


def activate(db,stage_file,report_path,ack):
    if not ack:raise RuntimeError('Activation requires --acknowledge-verified-five-table-cutover')
    db=db.resolve(strict=True)
    stage_file=stage_file.resolve(strict=True)
    report=json.loads(report_path.resolve(strict=True).read_text())
    if report.get('source_db')!=str(db) or report.get('stage_db')!=str(stage_file):
        raise RuntimeError('Manifest source/stage path mismatch')
    if report.get('python_modules_depending_on_legacy_tables'):
        raise RuntimeError('Runtime still depends on retired tables: '+repr(report['python_modules_depending_on_legacy_tables']))
    archive=Path(report['archive_db'])
    if not archive.is_file() or sha256(archive)!=report['archive_sha256']:
        raise RuntimeError('Complete original archive missing or changed')
    if sha256(stage_file)!=report['stage_sha256']:
        raise RuntimeError('Staged database altered since verification')
    if shutil.disk_usage(db.parent).free < int(stage_file.stat().st_size * 1.2) + 50*1024*1024:
        raise RuntimeError('Not enough disk space next to the live database for an atomic staged copy')
    with ro(stage_file) as c:
        verify_integrity(c)
        if tables(c)!=KEEP or counts(c,KEEP)!=report['retained_counts']:
            raise RuntimeError('Stage is not the verified five-table DB')
    busy(db)
    if sha256(db)!=report.get('source_sha256'):
        raise RuntimeError('Live DB bytes changed since staging; rerun stage before activation')
    # Refuse if the original changed since staging; no silent stale cutovers.
    with ro(db) as c:
        verify_integrity(c)
        if counts(c,tables(c))!=report['original_counts']:
            raise RuntimeError('Source row counts changed since staging; restage')
    sidecars=[Path(str(db)+x) for x in ('-wal','-shm')]
    if any(x.exists() and x.stat().st_size for x in sidecars):
        raise RuntimeError('Nonempty WAL/SHM files present; checkpoint offline and restage')
    # Compare original's bytes to archive only for DELETE-mode DBs: archive uses
    # SQLite backup and may differ bytewise; row counts plus offline fuser check
    # are the validated safety contract. Preserve original independently.
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    next_db=db.with_name(db.name+f'.five-table-next-{stamp}')
    old=db.with_name(db.name+f'.before-five-table-{stamp}')
    unused(next_db);unused(old)
    shutil.copy2(stage_file,next_db)
    if sha256(next_db)!=report['stage_sha256']:
        next_db.unlink(missing_ok=True)
        raise RuntimeError('Staged copy differs after transfer')
    busy(db)
    os.replace(db,old)
    try:
        os.replace(next_db,db)
    except BaseException:
        os.replace(old,db)
        raise
    with ro(db) as c:
        verify_integrity(c)
        if tables(c)!=KEEP:
            raise RuntimeError('Post-cutover five-table check failed; stop all clients and restore original')
    print('ACTIVATED DATABASE:',db)
    print('ORIGINAL ON SAME FILESYSTEM:',old)
    print('COMPLETE EXTERNAL BACKUP:',archive)
    print('Before restarting services, deploy the matching canonical-only backend source code.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    for command in ('stage','activate'):
        x=sub.add_parser(command)
        x.add_argument('--db',type=Path,default=Path('/opt/atlas/data/edgar.sqlite3'))
        if command=='stage':
            x.add_argument('--out-dir',type=Path,default=Path('/opt/atlas/runtime/database'))
            x.add_argument('--source-root',type=Path,default=Path('/opt/atlas/main_back_end'))
        else:
            x.add_argument('--stage-file',type=Path,required=True)
            x.add_argument('--report',type=Path,required=True)
            x.add_argument('--acknowledge-verified-five-table-cutover',action='store_true')
    a=p.parse_args()
    if sqlite3.sqlite_version_info < (3,35):
        raise RuntimeError('SQLite >= 3.35 required to drop relationship columns')
    if a.action=='stage':stage(a.db,a.out_dir,a.source_root)
    else:activate(a.db,a.stage_file,a.report,a.acknowledge_verified_five_table_cutover)

if __name__=='__main__':
    try:main()
    except (RuntimeError, sqlite3.Error, OSError, KeyError, ValueError) as e:
        print('ERROR:',e,file=sys.stderr)
        raise SystemExit(1)
