"""SEC collector persistence targeting five canonical business tables only."""
from __future__ import annotations
from shared.processing_log import log_step
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .settings import FORMS, FORM_DESCRIPTIONS, WATCH_8K_ITEMS
from .parsing import Filing, utc_iso

AI_DEFAULTS = {
 'ai_impact':0,'ai_confidence':0,'ai_reason':'Not analysed yet.',
 'ai_evidence':'Not stated','ai_monetary_effect':0,
 'ai_monetary_effect_reason':'Not analysed yet. 0 USD is a placeholder, not a measured effect.',
 'price_change_1w_pct':0,
 'price_change_reason':'Not analysed yet. 0% is a placeholder, not a market forecast.',
 'errors':'',
}

def now_iso():
    return utc_iso(datetime.now(timezone.utc))

def backup_database(conn, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as dst:
        conn.backup(dst)

def connect(db_path:Path|str) -> sqlite3.Connection:
    """Open canonical SQLite without changing journal mode or running migrations.

    SQLite serializes overlapping write transactions even when multiple hourly
    pipelines run concurrently. Wait for short-lived writers (configurable busy
    timeout); never switch journal mode while live API/dashboard clients are open.
    WAL setup and schema changes belong to offline administration.
    """
    from shared.config import integer as config_int
    from shared.canonical_schema import ensure_five_schema
    from dashboard.canonical_reader import CanonicalConnection

    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = config_int('ATLAS_SQLITE_BUSY_TIMEOUT_MS', 60000,
                            minimum=1000, maximum=120000)
    raw = sqlite3.connect(path, timeout=timeout_ms / 1000)
    try:
        raw.row_factory = sqlite3.Row
        raw.execute(f'PRAGMA busy_timeout={timeout_ms}')
        # For an existing five-table DB this performs only schema reads and
        # enables foreign keys. A genuinely empty DB may be initialized.
        ensure_five_schema(raw)
        required = {'acumm_impact', 'acumm_monetary', 'accum_confdent'}
        columns = {r[1] for r in raw.execute('PRAGMA table_info(companies)')}
        missing = required - columns
        if missing:
            raise RuntimeError('Atlas companies table lacks required analytics columns '
                               f'{sorted(missing)}; perform an explicit, backed-up '
                               'offline schema migration. Runtime extraction will not '
                               'copy or alter a live database schema.')
    except BaseException as exc:
        raw.close()
        if isinstance(exc, sqlite3.OperationalError) and any(
                word in str(exc).lower() for word in ('locked', 'busy')):
            raise sqlite3.OperationalError(
                f'Atlas database busy after {timeout_ms} ms: {path}. '
                'Check active writers with fuser -v on the DB and its -wal/-shm '
                'files; do not delete SQLite sidecar files.') from exc
        raise
    return CanonicalConnection(raw)


def upsert_company(conn,cik,name,ticker=None,exchange=None,source='SEC filing'):
    """SEC CIK remains a UNIQUE business identifier; id is the surrogate PK."""
    cik=str(cik).zfill(10)
    old=conn.execute('SELECT name,ticker,exchange FROM companies WHERE cik=?',(cik,)).fetchone()
    ts=now_iso()
    if old is None:
        conn.execute('INSERT INTO companies(cik,name,ticker,exchange,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                     (cik,name or cik,ticker,exchange,ts,ts))
    else:
        new_name=(name or old['name']) if source=='SEC filing' else old['name']
        new_ticker=ticker if ticker is not None else old['ticker']
        new_exchange=exchange if exchange is not None else old['exchange']
        if (new_name,new_ticker,new_exchange)!=(old['name'],old['ticker'],old['exchange']):
            conn.execute('UPDATE companies SET name=?,ticker=?,exchange=?,updated_at=? WHERE cik=?',
                         (new_name,new_ticker,new_exchange,ts,cik))
    company_id=conn.execute('SELECT id FROM companies WHERE cik=?',(cik,)).fetchone()[0]
    if old is None or (new_name,new_ticker,new_exchange)!=(old['name'],old['ticker'],old['exchange']):
        log_step('sec.company','created' if old is None else 'updated',company_id=company_id,cik=cik,name=name,ticker=ticker,exchange=exchange,source=source)
    return company_id


def add_form(conn, filing:Filing)->str:
    """Idempotently store SEC filings and company linkage in canonical tables."""
    from shared.canonical_schema import metadata
    if filing.form_type not in FORMS: return 'excluded'
    if filing.form_type=='8-K' and not(set(filing.items)&WATCH_8K_ITEMS): return 'excluded'
    if conn.execute("SELECT 1 FROM raw_items WHERE source_type='filing' AND source_url=?",
                    (filing.index_url,)).fetchone(): return 'already_seen'
    stamp=now_iso()
    with conn:
        company_id=upsert_company(conn,filing.cik,filing.company_name)
        cur=conn.execute("""INSERT OR IGNORE INTO raw_items
           (source_type,source_key,title,event_at,received_at,source_url,source_name,
            status,metadata_json,created_at,updated_at)
           VALUES('filing',?,?,?,?,?,?,'created',?,?,?)""",
           (filing.index_url, FORM_DESCRIPTIONS[filing.form_type], filing.filing_ts_utc,
            stamp, filing.index_url,'SEC',
            metadata({'company_id':company_id,'form_type':filing.form_type,
                      'form_name':FORM_DESCRIPTIONS[filing.form_type], 'stage':'created'}),stamp,stamp))
        if cur.rowcount:
            source_id=cur.lastrowid
            conn.execute("""UPDATE raw_items SET metadata_json=json_set(metadata_json,'$.id',?)
                  WHERE id=?""",(source_id,source_id))
            company=conn.execute('SELECT name FROM companies WHERE id=?',(company_id,)).fetchone()
            conn.execute("""INSERT OR IGNORE INTO relationships
              (company_id,source_id,entity_type,entity_key,entity_name,created_at,updated_at)
              VALUES(?,?,'company',?,?,?,?)""",
              (company_id,source_id,'company:'+str(company_id),company['name'],stamp,stamp))
    return 'inserted' if cur.rowcount else 'already_seen'


def _filing_source_id(conn, form_id):
    row=conn.execute("""SELECT id FROM raw_items WHERE source_type='filing'
       AND (id=? OR CAST(json_extract(metadata_json,'$.id') AS INTEGER)=?)""",
       (form_id,form_id)).fetchone()
    if row is None: raise ValueError('Unknown form id')
    return row['id']


def set_form_error(conn,form_id,message,*,leave_extracted=False):
    with conn:
        conn.execute("""UPDATE raw_items SET metadata_json=json_set(metadata_json,
             '$.error',?,'$.stage',?),updated_at=? WHERE id=?""",
           (str(message)[:500], 'extracted' if leave_extracted else 'error',now_iso(),
            _filing_source_id(conn,form_id)))


def store_document(conn,form_id,*,url,guid,filename):
    with conn:
        conn.execute("""UPDATE raw_items SET metadata_json=json_set(metadata_json,
           '$.primary_url',?,'$.file_guid',?,'$.file_name',?,'$.stage','extracted',
           '$.error',''),updated_at=? WHERE id=?""",
           (url,guid,filename,now_iso(),_filing_source_id(conn,form_id)))


def store_items(conn,form_id,guid,parsed_items):
    """Upsert parsed SEC Items directly, retaining assessments for identical text."""
    from shared.canonical_schema import metadata
    form=conn.execute("""SELECT s.id,s.source_key,s.event_at,s.source_url,s.source_name,
          s.metadata_json, r.company_id, c.name company_name
      FROM raw_items s JOIN relationships r ON r.source_id=s.id
      JOIN companies c ON c.id=r.company_id
      WHERE s.source_type='filing' AND (s.id=? OR
          CAST(json_extract(s.metadata_json,'$.id') AS INTEGER)=?)
      LIMIT 1""",(form_id,form_id)).fetchone()
    if form is None: raise ValueError('Unknown filing')
    import json
    fm=json.loads(form['metadata_json'] or '{}')
    form_type=fm.get('form_type')
    filtered=[p for p in parsed_items if p.item_code!='9.01' and
              (form_type!='8-K' or p.item_code in WATCH_8K_ITEMS)]
    if not filtered: raise ValueError('No selected substantive Items found in report')
    ts=now_iso();counts={};identifiers=[]
    with conn:
        for entry in filtered:
            code=entry.item_code;counts[code]=counts.get(code,0)+1
            ordinal=counts[code]
            title=(entry.part+' · ' if entry.part else '')+entry.item_name
            sk=f"item:{form['id']}:{code}:{ordinal}"
            # Reuse migrated numeric source keys for identical original filing
            # identity/type/occurrence instead of creating duplicate Items.
            existing=conn.execute("""SELECT id,source_key,content FROM raw_items
                WHERE source_type='item' AND
                 (source_key=? OR (
                    CAST(json_extract(metadata_json,'$.form_id') AS INTEGER)=?
                    AND json_extract(metadata_json,'$.item_type')=?
                    AND CAST(json_extract(metadata_json,'$.item_occurrence') AS INTEGER)=?))
                ORDER BY CASE WHEN source_key=? THEN 0 ELSE 1 END LIMIT 1""",
                (sk,form_id,code,ordinal,sk)).fetchone()
            if existing: sk=existing['source_key']
            identifiers.append(sk)
            properties={'form_id':form_id,'file_guid':guid,'file_name':fm.get('file_name'),
                        'item_type':code,'item_occurrence':ordinal,'stage':'processed'}
            conn.execute("""INSERT INTO raw_items
             (source_type,source_key,parent_key,title,content,event_at,received_at,
              source_url,source_name,status,metadata_json,created_at,updated_at)
             VALUES('item',?,?,?,?,?,?,?,?,'created',?,?,?)
             ON CONFLICT(source_type,source_key) DO UPDATE SET
              title=excluded.title,content=excluded.content,
              status=CASE WHEN raw_items.content=excluded.content THEN raw_items.status
                       ELSE 'created' END,
              metadata_json=CASE WHEN raw_items.content=excluded.content THEN
                json_set(raw_items.metadata_json,'$.stage',
                   COALESCE(json_extract(raw_items.metadata_json,'$.stage'),'processed'))
                ELSE excluded.metadata_json END,updated_at=excluded.updated_at""",
             (sk,form['source_key'],title,entry.item_text,form['event_at'],ts,
              form['source_url'],form_type,metadata(properties),ts,ts))
            current=conn.execute("SELECT id,content FROM raw_items WHERE source_type='item' AND source_key=?",(sk,)).fetchone()
            if existing and existing['content']!=entry.item_text:
                conn.execute("""UPDATE relationships SET ai_impact=NULL,ai_confidence=NULL,
                      ai_reason=NULL,ai_evidence=NULL,ai_monetary_effect=NULL,
                      ai_monetary_effect_reason=NULL,price_change_1w_pct=NULL,
                      price_change_reason=NULL,updated_at=? WHERE source_id=?""",(ts,current['id']))
            conn.execute("""INSERT INTO relationships
                 (company_id,source_id,entity_type,entity_key,entity_name,created_at,updated_at)
               VALUES(?,?,'company',?,?,?,?)
               ON CONFLICT(source_id,entity_key) DO NOTHING""",
              (form['company_id'],current['id'],'company:'+str(form['company_id']),
               form['company_name'],ts,ts))
        # Obsolete item occurrences are deleted only for the same reparsed filing.
        if identifiers:
            placeholders=','.join('?' for _ in identifiers)
            conn.execute(f"""DELETE FROM raw_items WHERE source_type='item' AND parent_key=?
                AND source_key NOT IN ({placeholders})""",(form['source_key'],*identifiers))
        conn.execute("""UPDATE raw_items SET status='connected',
          metadata_json=json_set(metadata_json,'$.stage','parsed','$.error',''),
          updated_at=? WHERE id=?""",(ts,form['id']))
    return len(filtered)
