"""Canonical company/source/assessment projection of existing News and SEC writers.

News ingestion and SEC parsing retain their dedicated staging tables while those
pipelines are being ported. The unified *business* schema is raw_items,
relationships, summary and companies. A lossless SQLite metadata JSON image of
source fields is retained in raw_items/relationships, including filing metadata; text bodies live only in the common content column.

This code never silently deletes old tables or price bars. The migration's
mandatory online backup and verification are owned by the CLI.
"""
from __future__ import annotations
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS raw_items (
 id INTEGER PRIMARY KEY,
 source_type TEXT NOT NULL CHECK(source_type IN ('news','filing','item')),
 source_key TEXT NOT NULL,
 parent_key TEXT,
 title TEXT NOT NULL DEFAULT '',
 content TEXT NOT NULL DEFAULT '',
 summary TEXT,
 event_at TEXT,
 received_at TEXT,
 source_url TEXT,
 source_name TEXT,
 status TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','skipped','connected','analysed')),
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT,
 updated_at TEXT,
 UNIQUE(source_type,source_key)
);
CREATE INDEX IF NOT EXISTS idx_raw_type_event ON raw_items(source_type,event_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_parent ON raw_items(parent_key,source_type);
CREATE TABLE IF NOT EXISTS relationships (
 id INTEGER PRIMARY KEY,
 company_id INTEGER REFERENCES companies(id),
 source_id INTEGER NOT NULL REFERENCES raw_items(id) ON DELETE CASCADE,
 entity_type TEXT NOT NULL DEFAULT 'company',
 entity_key TEXT NOT NULL,
 entity_name TEXT NOT NULL DEFAULT '',
 correlation_reason TEXT,
 match_evidence TEXT,
 match_hits INTEGER NOT NULL DEFAULT 0,
 ai_impact REAL,
 ai_confidence REAL,
 ai_reason TEXT,
 ai_evidence TEXT,
 ai_monetary_effect REAL,
 ai_monetary_effect_reason TEXT,
 price_change_1w_pct REAL,
 price_change_reason TEXT,
 accumulated_impact REAL,
 accumulated_monetary REAL,
 legacy_relation_id TEXT,
 legacy_item_id INTEGER,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT,
 updated_at TEXT,
 UNIQUE(source_id,entity_key)
);
CREATE INDEX IF NOT EXISTS idx_rel_company ON relationships(company_id);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_legacy_news ON relationships(legacy_relation_id)
 WHERE legacy_relation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_legacy_item ON relationships(legacy_item_id)
 WHERE legacy_item_id IS NOT NULL;
'''


def cols(conn, table):
    return [r[1] for r in conn.execute('PRAGMA table_info("'+table+'")')]


def available(conn):
    names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {'companies','news','forms','items','news_company_relations'} <= names


def json_row(conn, table, alias):
    """Set-based SQLite JSON for all source-specific fields. Bodies live in content."""
    names=[x for x in cols(conn,table) if x not in ('status','analysed_at','accumulated_event_count','accumulated_news_count')
           and not (table=='news' and x=='article_text')
           and not (table=='items' and x=='item_text')]
    if len(names)>60:
        raise RuntimeError('Too many fields for lossless legacy JSON: '+table)
    return "json_object(" + ",".join("'"+x+"',"+alias+'."'+x+'"' for x in names) + ")"


def source_specs(conn):
    n=json_row(conn,'news','n');f=json_row(conn,'forms','f');i=json_row(conn,'items','i')
    # Every raw item has exactly one of the four standard workflow statuses.
    # Legacy collection/extraction-specific states map to `created` until linked.
    def normal_status(expr):
        return f"CASE WHEN {expr}='analysed' THEN 'analysed' WHEN {expr} IN ('skipped','rejected','filtered') THEN 'skipped' WHEN {expr}='connected' THEN 'connected' ELSE 'created' END"
    return {
        'news': ('''news n''',
          "'news'", 'n.news_id', 'NULL','n.title', 'n.article_text','n.summary',
          'n.published_utc','n.created_at','n.url','n.source',normal_status('n.status'),n,'n.created_at','n.updated_at'),
        'filing': ('''forms f''',
          "'filing'",'CAST(f.id AS TEXT)','NULL','f.form_name',"''",'NULL',
          'f.filed_date','f.created_at','f.index_url',"'SEC'",normal_status('f.status'),f,'f.created_at','f.updated_at'),
        'item': ('''items i JOIN forms f ON f.id=i.form_id''',
          "'item'",'CAST(i.id AS TEXT)','f.index_url','i.item_name','i.item_text','NULL',
          'f.filed_date','i.created_at','COALESCE(f.primary_url,f.index_url)','f.form_type',normal_status('i.status'),i,'i.created_at','i.updated_at'),
    }


RAW_COLS=('source_type','source_key','parent_key','title','content','summary',
          'event_at','received_at','source_url','source_name','status',
          'metadata_json','created_at','updated_at')


def raw_upsert(conn, kind, where=None):
    spec=source_specs(conn)[kind]
    frm=spec[0]; values=spec[1:]
    where_sql=(' WHERE '+where) if where else ' WHERE 1=1'
    assignments=','.join(c+'=excluded.'+c for c in RAW_COLS if c not in ('source_type','source_key'))
    return ('INSERT INTO raw_items ('+','.join(RAW_COLS)+') SELECT '+','.join(values)+' FROM '+frm+where_sql+
            ' ON CONFLICT(source_type,source_key) DO UPDATE SET '+assignments)


def news_rel_upsert(conn, where=None):
    extra=' WHERE '+where if where else ''
    meta=json_row(conn,'news_company_relations','r')
    fields='''company_id,source_id,entity_type,entity_key,entity_name,correlation_reason,
      match_evidence,match_hits,ai_impact,ai_confidence,ai_reason,ai_evidence,ai_monetary_effect,
      ai_monetary_effect_reason,price_change_1w_pct,price_change_reason,
      legacy_relation_id,legacy_item_id,metadata_json,created_at,updated_at'''
    values=f'''r.company_id,s.id,r.entity_type,r.entity_key,r.entity_name,
       r.correlation_reason,r.match_evidence,r.match_hits,
       CASE WHEN r.status='analysed' THEN r.ai_impact END,
       CASE WHEN r.status='analysed' THEN r.ai_confidence END,
       CASE WHEN r.status='analysed' THEN r.ai_reason END,
       CASE WHEN r.status='analysed' THEN r.ai_evidence END,
       CASE WHEN r.status='analysed' THEN r.ai_monetary_effect END,
       CASE WHEN r.status='analysed' THEN r.ai_monetary_effect_reason END,
       CASE WHEN r.status='analysed' THEN r.price_change_1w_pct END,
       CASE WHEN r.status='analysed' THEN r.price_change_reason END,
       r.relation_id,NULL,{meta},r.created_at,r.updated_at'''
    update=','.join(f'{k}=excluded.{k}' for k in fields.replace('\n','').replace(' ','').split(',') if k not in ('source_id','entity_key'))
    return f'''INSERT INTO relationships ({fields}) SELECT {values}
       FROM news_company_relations r JOIN raw_items s
         ON s.source_type='news' AND s.source_key=r.news_id{extra}
       ON CONFLICT(source_id,entity_key) DO UPDATE SET {update}'''


def sec_rel_upsert(conn, where=None):
    meta=json_row(conn,'items','i')
    where_sql=' WHERE '+where if where else ''
    fields='''company_id,source_id,entity_type,entity_key,entity_name,
      ai_impact,ai_confidence,ai_reason,ai_evidence,ai_monetary_effect,
      ai_monetary_effect_reason,price_change_1w_pct,price_change_reason,
      legacy_relation_id,legacy_item_id,metadata_json,created_at,updated_at'''
    values=f'''f.company_id,s.id,'company','company:'||f.company_id,c.name,
       CASE WHEN i.status='analysed' THEN i.ai_impact END,
       CASE WHEN i.status='analysed' THEN i.ai_confidence END,
       CASE WHEN i.status='analysed' THEN i.ai_reason END,
       CASE WHEN i.status='analysed' THEN i.ai_evidence END,
       CASE WHEN i.status='analysed' THEN i.ai_monetary_effect END,
       CASE WHEN i.status='analysed' THEN i.ai_monetary_effect_reason END,
       CASE WHEN i.status='analysed' THEN i.price_change_1w_pct END,
       CASE WHEN i.status='analysed' THEN i.price_change_reason END,
       NULL,i.id,{meta},i.created_at,i.updated_at'''
    update=','.join(k+'=excluded.'+k for k in fields.replace('\n','').replace(' ','').split(',') if k not in ('source_id','entity_key'))
    return f'''INSERT INTO relationships ({fields}) SELECT {values}
       FROM items i JOIN forms f ON f.id=i.form_id JOIN companies c ON c.id=f.company_id
       JOIN raw_items s ON s.source_type='item' AND s.source_key=CAST(i.id AS TEXT)
       {where_sql} ON CONFLICT(source_id,entity_key) DO UPDATE SET {update}'''


def filing_rel_upsert(conn, where=None):
    # Preserve filing display and company linkage; filings are not scored in
    # addition to their extracted items (no double-counting).
    where_sql=' WHERE '+where if where else ''
    meta=json_row(conn,'forms','f')
    return f'''INSERT INTO relationships (
       company_id,source_id,entity_type,entity_key,entity_name,
       legacy_relation_id,metadata_json,created_at,updated_at)
      SELECT f.company_id,s.id,'company','company:'||f.company_id,c.name,
             NULL,{meta},f.created_at,f.updated_at
      FROM forms f JOIN companies c ON c.id=f.company_id
      JOIN raw_items s ON s.source_type='filing' AND s.source_key=CAST(f.id AS TEXT)
      {where_sql}
      ON CONFLICT(source_id,entity_key) DO UPDATE SET
         company_id=excluded.company_id,entity_name=excluded.entity_name,
         metadata_json=excluded.metadata_json,updated_at=excluded.updated_at'''


def news_source_status_sql(news_key_expr):
    """Source-wide workflow; all linked company assessments must finish."""
    return f"""UPDATE raw_items SET status=CASE
      WHEN EXISTS(SELECT 1 FROM news_company_relations l WHERE l.news_id={news_key_expr}
        AND l.entity_type='company' AND l.status NOT IN ('analysed','skipped','rejected')) THEN 'connected'
      WHEN EXISTS(SELECT 1 FROM news_company_relations l WHERE l.news_id={news_key_expr}
        AND l.entity_type='company' AND l.status='analysed') THEN 'analysed'
      WHEN EXISTS(SELECT 1 FROM news_company_relations l WHERE l.news_id={news_key_expr}
        AND l.entity_type='company') THEN 'skipped'
      WHEN status='skipped' THEN 'skipped' ELSE 'created' END
      WHERE source_type='news' AND source_key={news_key_expr}"""


def recalculate_sql(cid_sql='SELECT id FROM companies'):
    # Only analyzed assessments contribute. Per-company completion is inferred
    # from saved ai_impact (which can legitimately be 0), not a second status.
    rows=f'''SELECT r.id rid,r.company_id cid,s.source_type kind,s.event_at stamp,
                    r.ai_impact impact,r.ai_monetary_effect monetary
             FROM relationships r JOIN raw_items s ON s.id=r.source_id
             WHERE r.company_id IN ({cid_sql}) AND r.entity_type='company'
               AND r.ai_impact IS NOT NULL AND s.status!='skipped'
               AND s.source_type IN ('news','item')'''
    ordering='CASE WHEN julianday(stamp) IS NULL THEN 0 ELSE 1 END,julianday(stamp),kind,rid'
    scored=f'''SELECT rid,cid,
     SUM(impact) OVER(PARTITION BY cid ORDER BY {ordering} ROWS UNBOUNDED PRECEDING) cumulative,
     SUM(COALESCE(monetary,0)) OVER(PARTITION BY cid ORDER BY {ordering} ROWS UNBOUNDED PRECEDING) cumulative_money
     FROM ({rows})'''
    return f'''UPDATE relationships
       SET accumulated_impact=NULL,accumulated_monetary=NULL
       WHERE company_id IN ({cid_sql}) AND (ai_impact IS NULL
          OR entity_type!='company' OR EXISTS (SELECT 1 FROM raw_items s
            WHERE s.id=relationships.source_id AND (s.status='skipped' OR s.source_type NOT IN ('news','item'))))
         AND (accumulated_impact IS NOT NULL OR accumulated_monetary IS NOT NULL);
       UPDATE relationships SET
        accumulated_impact=h.cumulative,accumulated_monetary=h.cumulative_money
       FROM ({scored}) AS h WHERE relationships.id=h.rid AND
         (relationships.accumulated_impact IS NOT h.cumulative OR
          relationships.accumulated_monetary IS NOT h.cumulative_money);'''


def backfill(conn):
    if not available(conn):raise RuntimeError('Missing News, filing or SEC tables: cannot preserve data')
    for statement in SCHEMA.split(';'):
        if statement.strip():conn.execute(statement)
    for kind in ('news','filing','item'):
        conn.execute(raw_upsert(conn,kind))
    conn.execute(news_rel_upsert(conn))
    conn.execute(filing_rel_upsert(conn))
    conn.execute(sec_rel_upsert(conn))
    conn.execute("UPDATE raw_items SET status='connected' WHERE source_type='filing' AND status='created'")
    conn.execute("""UPDATE raw_items SET status=CASE
      WHEN EXISTS (SELECT 1 FROM news_company_relations l WHERE l.news_id=raw_items.source_key
       AND l.entity_type='company' AND l.status NOT IN ('analysed','skipped','rejected')) THEN 'connected'
      WHEN EXISTS (SELECT 1 FROM news_company_relations l WHERE l.news_id=raw_items.source_key
       AND l.entity_type='company' AND l.status='analysed') THEN 'analysed'
      WHEN EXISTS (SELECT 1 FROM news_company_relations l WHERE l.news_id=raw_items.source_key
       AND l.entity_type='company') THEN 'skipped'
      ELSE status END WHERE source_type='news'""")
    for statement in recalculate_sql().split(';'):
        if statement.strip():conn.execute(statement)


def install_mirror_triggers(conn):
    """Compatibility bridge: legacy ingestion writes feed the canonical model.

    Old staging tables are retained until their writers are retired; this keeps
    future content, score updates and cumulative figures in sync immediately.
    """
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_unified_%'").fetchall():
        conn.execute('DROP TRIGGER "'+name+'"')
    def create(name,after,source,body):
        conn.execute(f'CREATE TRIGGER atlas_unified_{name} AFTER {after} ON {source} BEGIN {body} END')
    # New/edited source content updates raw metadata, including publication date.
    for name,kind,table,key in (('news','news','news','news_id'),('filing','filing','forms','id'),('item','item','items','id')):
        for event in ('INSERT','UPDATE'):
            check=f'{table[0] if False else {"news":"n","forms":"f","items":"i"}[table]}.{key}=NEW.{key}'
            body=raw_upsert(conn,kind,check)+';'
            if kind=='news':
                body+=news_source_status_sql('NEW.news_id')+'; '
                body+=recalculate_sql("SELECT DISTINCT company_id FROM relationships WHERE source_id="+
                  "(SELECT id FROM raw_items WHERE source_type='news' AND source_key=NEW.news_id)")
            if kind=='filing':
                body+=' '+filing_rel_upsert(conn,'f.id=NEW.id')+'; '
                body+="UPDATE raw_items SET status='connected' WHERE source_type='filing' AND source_key=CAST(NEW.id AS TEXT) AND status='created'; "
                body+=' '+raw_upsert(conn,'item','i.form_id=NEW.id')+'; '
                body+=' '+sec_rel_upsert(conn,'i.form_id=NEW.id')+'; '
                body+=recalculate_sql('SELECT NEW.company_id UNION SELECT OLD.company_id' if event=='UPDATE' else 'SELECT NEW.company_id')
            if kind=='item':
                body+=' '+sec_rel_upsert(conn,'i.id=NEW.id')+'; '
                body+=recalculate_sql('SELECT f.company_id FROM forms f WHERE f.id=NEW.form_id')
            create(name+'_'+event.lower(),event,table,body)
    # Company linkage and assessment state flow into one common relationship.
    for event in ('INSERT','UPDATE'):
        body=news_rel_upsert(conn,'r.relation_id=NEW.relation_id')+'; '
        body+=news_source_status_sql('NEW.news_id')+'; '
        company_scope=('SELECT OLD.company_id UNION SELECT NEW.company_id') if event=='UPDATE' else 'SELECT NEW.company_id'
        body+=recalculate_sql(company_scope)
        create('news_relation_'+event.lower(),event,'news_company_relations',body)
    # Deleting source-specific links mirrors the deletion, then repairs history.
    create('news_relation_delete','DELETE','news_company_relations',
       "DELETE FROM relationships WHERE legacy_relation_id=OLD.relation_id;"+
       news_source_status_sql('OLD.news_id')+';'+
       recalculate_sql('SELECT OLD.company_id'))
    create('item_delete','DELETE','items',
       "DELETE FROM relationships WHERE legacy_item_id=OLD.id;"+
       "UPDATE raw_items SET status='skipped',metadata_json=json_set(metadata_json,'$.atlas_deleted',1) WHERE source_type='item' AND source_key=CAST(OLD.id AS TEXT);"+
       recalculate_sql('SELECT f.company_id FROM forms f WHERE f.id=OLD.form_id'))
    # Source deletion leaves the database's audit-oriented raw items intact, but
    # removes relationships that are now meaningless.
    for name,table,kind,key in (('news_delete','news','news','news_id'),('filing_delete','forms','filing','id')):
        create(name,'DELETE',table,
           "UPDATE raw_items SET status='skipped',metadata_json=json_set(metadata_json,'$.atlas_deleted',1) WHERE source_type='"+kind+"' AND source_key=CAST(OLD."+key+" AS TEXT);"+
           "UPDATE relationships SET ai_impact=NULL,ai_monetary_effect=NULL,accumulated_impact=NULL,accumulated_monetary=NULL WHERE source_id=(SELECT id FROM raw_items WHERE source_type='"+kind+"' AND source_key=CAST(OLD."+key+" AS TEXT));")
    # Generic recalculation on direct canonical writes; only scoring fields
    # trigger it (updates to derived fields cannot loop).
    create('relationship_insert','INSERT','relationships',recalculate_sql('SELECT NEW.company_id'))
    create('relationship_update','UPDATE OF ai_impact,ai_monetary_effect,company_id,source_id','relationships',
           recalculate_sql('SELECT OLD.company_id UNION SELECT NEW.company_id'))
    create('relationship_delete','DELETE','relationships',recalculate_sql('SELECT OLD.company_id'))
    create('raw_status_update','UPDATE OF status','raw_items',
       recalculate_sql('SELECT company_id FROM relationships WHERE source_id=NEW.id'))
    # The original post-migration table was renamed in place, so unlike newly
    # created tables its status column has no CHECK. Enforce the same contract.
    conn.execute("""CREATE TRIGGER atlas_unified_raw_status_guard_insert
      BEFORE INSERT ON raw_items
      WHEN NEW.status NOT IN ('created','skipped','connected','analysed')
      BEGIN SELECT RAISE(ABORT,'Invalid raw_items.status'); END""")
    conn.execute("""CREATE TRIGGER atlas_unified_raw_status_guard_update
      BEFORE UPDATE OF status ON raw_items
      WHEN NEW.status NOT IN ('created','skipped','connected','analysed')
      BEGIN SELECT RAISE(ABORT,'Invalid raw_items.status'); END""")


def integrity_counts(conn):
    return {kind:conn.execute("SELECT COUNT(*) FROM raw_items WHERE source_type=? AND json_extract(metadata_json,'$.atlas_deleted') IS NOT 1",(kind,)).fetchone()[0]
            for kind in ('news','filing','item')}


def validate(conn):
    expected={'news':'news','filing':'forms','item':'items'}
    counts=integrity_counts(conn)
    for kind,table in expected.items():
        n=conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        if counts[kind]!=n:
            raise RuntimeError(f'{kind} data mismatch: old {n}, raw_items {counts[kind]}')
    n=conn.execute('SELECT COUNT(*) FROM news_company_relations').fetchone()[0]
    mapped=conn.execute('SELECT COUNT(*) FROM relationships WHERE legacy_relation_id IS NOT NULL').fetchone()[0]
    if n!=mapped:raise RuntimeError(f'News relationships mismatch: {n} vs {mapped}')
    n=conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    mapped=conn.execute('SELECT COUNT(*) FROM relationships WHERE legacy_item_id IS NOT NULL').fetchone()[0]
    if n!=mapped:raise RuntimeError(f'SEC relationships mismatch: {n} vs {mapped}')
    n=conn.execute("""SELECT COUNT(*) FROM relationships r JOIN raw_items s ON s.id=r.source_id
      WHERE r.company_id IS NOT NULL AND r.entity_type='company' AND r.ai_impact IS NOT NULL AND s.status!='skipped'
        AND s.source_type IN ('news','item') AND r.accumulated_impact IS NULL""").fetchone()[0]
    if n:raise RuntimeError(f'{n} analyzed unified relationships missing cumulative impact')
    fk=conn.execute('PRAGMA foreign_key_check').fetchall()
    if fk:raise RuntimeError(f'Foreign-key violations: {fk[:5]}')
    return counts
