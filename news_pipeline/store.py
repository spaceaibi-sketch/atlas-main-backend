"""Canonical news ingestion and company-link persistence (five tables only)."""
from __future__ import annotations
import json
import uuid
from shared.processing_log import log_step
from shared.canonical_schema import ensure_five_schema, metadata
from shared.stale_created import skip_created
from sec_edgar.database import now_iso
from .dedup import normalized_title, normalized_url, story_hash

# Source identity remains the original news UUID. Article and relationship
# metadata preserve optional source-specific fields without shadow tables.
NEWS_SELECT = """SELECT s.source_key news_id,s.title,s.source_url url,
 COALESCE(json_extract(s.metadata_json,'$.url_key'),s.source_key) url_key,
 json_extract(s.metadata_json,'$.title_key') title_key,
 json_extract(s.metadata_json,'$.story_key') story_key,
 s.summary,s.content article_text,s.source_name source,
 COALESCE(json_extract(s.metadata_json,'$.region'),'') region,
 COALESCE(json_extract(s.metadata_json,'$.category'),'') category,
 s.event_at published_utc,
 COALESCE(json_extract(s.metadata_json,'$.companies'),'') companies,
 COALESCE(json_extract(s.metadata_json,'$.filtration_reason'),'') filtration_reason,
 CASE WHEN s.status='created' THEN COALESCE(json_extract(s.metadata_json,'$.stage'),'created')
      ELSE s.status END status,
 COALESCE(json_extract(s.metadata_json,'$.errors'),'') errors,
 s.created_at,s.updated_at FROM raw_items s WHERE s.source_type='news'"""


def ensure_schema(conn):
    ensure_five_schema(conn)


def _source(conn, news_id):
    row=conn.execute("SELECT id,status FROM raw_items WHERE source_type='news' AND source_key=?",
                     (news_id,)).fetchone()
    if row is None: raise ValueError('Unknown news source')
    return row


def save_extracted_companies(conn, news_id, candidates):
    names=[];seen=set()
    for name,_reason in candidates:
        name=str(name).strip()
        if not name or any(c in name for c in ('|','\n','\r')):
            raise ValueError('Company name contains a forbidden delimiter')
        if name.casefold() not in seen:
            names.append(name);seen.add(name.casefold())
    if names:
        status='extracted'
        updated=conn.execute("""UPDATE raw_items SET metadata_json=json_set(metadata_json,
         '$.stage','extracted','$.companies',?,'$.filtration_reason','','$.errors',''),
         updated_at=? WHERE source_type='news' AND source_key=? AND status='created'
         AND COALESCE(json_extract(metadata_json,'$.stage'),'created')='created'""",
         ('|'.join(names),now_iso(),news_id))
    else:
        status='skipped'
        updated=conn.execute("""UPDATE raw_items SET status='skipped',
          metadata_json=json_set(metadata_json,'$.companies','',
              '$.filtration_reason','No qualifying companies extracted.','$.errors',''),
          updated_at=? WHERE source_type='news' AND source_key=? AND status='created'""",
          (now_iso(),news_id))
    if updated.rowcount!=1: raise ValueError('Article not eligible for company extraction')
    return status


def add_story(conn, story):
    url_key=normalized_url(story.url);title_key=normalized_title(story.title)
    if not url_key or not title_key:
        log_step('news.ingest','invalid',title=story.title,url=story.url)
        return 'invalid',None
    if conn.execute("""SELECT 1 FROM raw_items WHERE source_type='news' AND
                      json_extract(metadata_json,'$.url_key')=?""",(url_key,)).fetchone():
        return 'duplicate_url',None
    now=now_iso(); news_id=str(uuid.uuid4())
    with conn:
        cursor=conn.execute("""INSERT OR IGNORE INTO raw_items
           (source_type,source_key,title,summary,content,event_at,received_at,
            source_url,source_name,status,metadata_json,created_at,updated_at)
           VALUES('news',?,?,?,'',?,?,?,?, 'created',?,?,?)""",
           (news_id,story.title,story.summary,story.published_utc,now,
            story.url,story.source,metadata({'url_key':url_key,'title_key':title_key,
                  'story_key':story_hash(story.title),'region':story.region,
                  'category':story.category,'companies':'','filtration_reason':'',
                  'errors':'','stage':'created'}),now,now))
    return ('inserted',news_id) if cursor.rowcount else ('duplicate_url',None)


def _window_seconds(now=None):
    from .time_window import NEWS_MAX_AGE,ensure_utc_now
    now=ensure_utc_now(now)
    return int((now-NEWS_MAX_AGE).timestamp()),int(now.timestamp())


def expire_pending(conn,now=None):
    """Skip News still 'created' more than 24 hours after created_at."""
    return skip_created(conn, sources=('news',), now=now)['news']


def pending_news(conn,limit=100,retry_errors=False,now=None,enforce_window=False):
    sql=f'SELECT * FROM ({NEWS_SELECT}) n WHERE n.status=\'created\''
    if not retry_errors: sql+=" AND errors=''"
    sql+=' ORDER BY published_utc DESC,created_at DESC'
    args=[]
    if limit>0: sql+=' LIMIT ?';args.append(int(limit))
    return [dict(x) for x in conn.execute(sql,args)]


def save_company_correlations(conn, news_id, resolved):
    stamp=now_iso();source=_source(conn,news_id)
    # Remove unscored provisional links only, retaining analyzed history.
    conn.execute("DELETE FROM relationships WHERE source_id=? AND ai_impact IS NULL",
                 (source['id'],))
    if not resolved:
        conn.execute("""UPDATE raw_items SET status='created',
           metadata_json=json_set(metadata_json,'$.stage','extracted')
           WHERE id=? AND status='connected'""",(source['id'],))
        return 0
    values=[(cid,source['id'],'CIK:'+cik,name,reason,stamp,stamp) for cid,cik,name,reason in resolved]
    conn.executemany("""INSERT INTO relationships
      (company_id,source_id,entity_type,entity_key,entity_name,correlation_reason,
       match_hits,match_evidence,created_at,updated_at)
      VALUES(?,?,'company',?,?,?,0,'AI contextual extraction',?,?)
      ON CONFLICT(source_id,entity_key) DO UPDATE SET
       correlation_reason=excluded.correlation_reason,match_evidence=excluded.match_evidence,
       updated_at=excluded.updated_at""",values)
    conn.execute("""UPDATE raw_items SET status='connected',updated_at=? WHERE id=?
                   AND status IN ('created','connected')""",(stamp,source['id']))
    return len(values)


def set_error(conn,news_id,reason):
    with conn:
        conn.execute("""UPDATE raw_items SET metadata_json=json_set(metadata_json,'$.errors',?),
          updated_at=? WHERE source_type='news' AND source_key=?""",
          (str(reason)[:500],now_iso(),news_id))


def news_statistics(conn,now=None):
    low,high=_window_seconds(now)
    statuses={'created':0,'skipped':0,'extracted':0,'connected':0,'analysed':0}
    recent=dict.fromkeys(statuses,0)
    for r in conn.execute(f"""SELECT n.status,COUNT(*) total,
      SUM(CASE WHEN CAST(strftime('%s',n.published_utc) AS INTEGER) BETWEEN ? AND ?
         THEN 1 ELSE 0 END) recent FROM ({NEWS_SELECT}) n GROUP BY n.status""",(low,high)):
        if r['status'] in statuses:
            statuses[r['status']]=r['total'];recent[r['status']]=r['recent']
    rel=conn.execute("""SELECT COUNT(*),COUNT(DISTINCT r.company_id)
         FROM relationships r JOIN raw_items s ON s.id=r.source_id
         WHERE s.source_type='news'""").fetchone()
    return {'total':sum(statuses.values()),'statuses':statuses,'last_3h':recent,
            'last_3h_total':sum(recent.values()),'relations':rel[0],
            'affected_companies':rel[1]}


def previous_main_news(conn,now=None,limit=70,exclude=()):
    low,high=_window_seconds(now)
    rows=conn.execute(f"""SELECT * FROM ({NEWS_SELECT}) n WHERE n.status
      IN ('extracted','connected','analysed') AND
      CAST(strftime('%s',n.published_utc) AS INTEGER) BETWEEN ? AND ?
      ORDER BY n.published_utc DESC LIMIT ?""",(low,high,max(1,limit*3))).fetchall()
    excluded=set(exclude)
    return [dict(x) for x in rows if x['news_id'] not in excluded][:limit]


def save_batch_groups(conn, rows, main_ids, duplicates):
    ids={r['news_id'] for r in rows}
    if set(main_ids)|set(duplicates)!=ids or set(main_ids)&set(duplicates):
        raise ValueError('Each article must be a main or duplicate')
    stamp=now_iso()
    for nid in main_ids:
        result=conn.execute("""UPDATE raw_items SET metadata_json=json_set(metadata_json,
           '$.errors',''),updated_at=? WHERE source_type='news' AND source_key=?
           AND status='created' AND COALESCE(json_extract(metadata_json,'$.stage'),'created')='created'""",
           (stamp,nid))
        if result.rowcount!=1: raise ValueError('Main article state changed')
    for nid in duplicates:
        source=_source(conn,nid)
        conn.execute('DELETE FROM relationships WHERE source_id=? AND ai_impact IS NULL',
                     (source['id'],))
        result=conn.execute("""UPDATE raw_items SET status='skipped',
          metadata_json=json_set(metadata_json,'$.filtration_reason',
              'Duplicate coverage; skipped.','$.companies','','$.errors',''),
          updated_at=? WHERE id=? AND status='created'""",(stamp,source['id']))
        if result.rowcount!=1:raise ValueError('Duplicate article state changed')
    return len(main_ids),len(duplicates)
