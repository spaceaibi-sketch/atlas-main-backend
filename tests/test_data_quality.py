"""Status visibility, three-hour semantics and zero-safe read-only reporting."""
from datetime import datetime, timezone

from dashboard.data_quality import quality_counts, render_quality
from news_pipeline.store import ensure_schema
from sec_edgar.database import connect, upsert_company

NOW = datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc)


def make_db(tmp_path):
    db = connect(tmp_path/'data.sqlite3')
    ensure_schema(db)
    return db


def test_quality_zero_database(tmp_path):
    db = make_db(tmp_path)
    report = quality_counts(db, NOW)
    assert report['groups']['filings']['total'] == 0
    assert report['groups']['items']['recent'] == 0
    assert report['groups']['news']['total'] == 0
    assert 'events' not in report['groups']  # retired batch_status is not a stage
    assert all(v == 0 for v in report['checks'].values())
    assert report['activity']['last_item_extracted'] is None
    db.close()


def test_quality_statuses_and_recent_windows(tmp_path):
    db = make_db(tmp_path)
    with db:
        cid = upsert_company(db, '0000000212', 'Sample Company Inc.')
        parsed = db.execute('''INSERT INTO forms
            (company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
            VALUES (?,'8-K','Report','2026-10-13T11:00:00Z',?,'parsed',?,?)''',
            (cid, 'https://example.org/a','2026-10-17T20:00:00Z','2026-10-17T21:00:00Z')).lastrowid
        old = db.execute('''INSERT INTO forms
            (company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
            VALUES (?,'10-Q','Quarterly','2026-10-13T11:00:00Z',?,'error',?,?)''',
            (cid, 'https://example.org/b','2026-10-13T20:00:00Z','2026-10-16T23:00:00Z')).lastrowid
        iid=db.execute('''INSERT INTO items
            (form_id,item_type,item_name,item_text,status,created_at,updated_at)
            VALUES (?,'8.01','Event','Substantial text','analysed',?,?)''',
            (parsed, '2026-10-17T20:05:00Z', '2026-10-17T21:30:00Z')).lastrowid
        db.execute('''INSERT INTO items
            (form_id,item_type,item_name,item_text,status,created_at,updated_at)
            VALUES (?,'2.02','Results','','rejected',?,?)''',
            (old,'2026-10-16T20:00:00Z','2026-10-16T20:00:00Z'))
        newsrows = [
           ('a1','Story A','2026-10-17T21:00:00Z','connected','main','2026-10-17T21:05:00Z'),
           ('a2','Story B','2026-10-16T17:00:00Z','skipped','duplicate','2026-10-16T17:05:00Z'),
           ('a3','Story C','2026-10-18T03:00:00Z','created','pending','2026-10-17T21:10:00Z'),
        ]
        for nid,title,published,status,batch,created in newsrows:
            db.execute('''INSERT INTO news
               (news_id,title,url,url_key,title_key,story_key,source,published_utc,status,
                created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (nid,title,'https://example.org/'+nid,'key-'+nid,title,'hash-'+nid,'Feed',published,
                 status,created,created))
        db.execute('''INSERT INTO news_company_relations
            (relation_id,news_id,company_id,entity_key,entity_type,entity_name,status,
             correlation_reason,created_at,updated_at)
            VALUES ('rel1','a1',?,'CIK:0000000212','company','Sample Company Inc.',
                    'connected','Contract confirms exposure',?,?)''',
            (cid,'2026-10-17T21:11:00Z','2026-10-17T21:11:00Z'))
    r=quality_counts(db, NOW)
    forms={x['code']:x for x in r['groups']['filings']['rows']}
    assert forms['parsed']['total']==forms['parsed']['recent']==1
    assert forms['error']['total']==1 and forms['error']['recent']==0
    its={x['code']:x for x in r['groups']['items']['rows']}
    assert (its['analysed']['total'], its['analysed']['recent'])==(1,1)
    assert (its['rejected']['total'], its['rejected']['recent'])==(1,0)
    assert its['analysed']['changed_at']=='2026-10-17T21:30:00Z'
    news={x['code']:x for x in r['groups']['news']['rows']}
    assert news['connected']['recent']==1
    assert news['created']['recent']==0 # tomorrow's scheduled publication excluded
    assert 'events' not in r['groups']
    assert r['groups']['links']['total']==r['groups']['links']['recent']==1
    assert r['activity']['last_item_extracted']=='2026-10-17T20:05:00Z'
    assert r['activity']['last_item_analysed']=='2026-10-17T21:30:00Z'
    assert r['activity']['last_news_published']=='2026-10-17T21:00:00Z'
    assert r['checks']['future_news']==1
    assert r['checks']['invalid_duplicates']==0  # A skipped duplicate no longer needs a main link
    assert r['checks']['items_failed']==1
    assert r['checks']['parsed_without_items']==0
    assert r['checks']['links_missing_reason']==0
    assert r['checks']['inconsistent_news_analysis']==0
    db.close()


def test_quality_missing_reason_and_parsed_without_items(tmp_path):
    db=make_db(tmp_path)
    with db:
        company=upsert_company(db,'0000000009','Target Company')
        fid=db.execute('''INSERT INTO forms
           (company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
           VALUES (?,'10-K','Annual','2026-10-17T19:00:00Z',?,'parsed',?,?)''',
           (company,'https://example.org/unparsed','2026-10-17T19:00:00Z','2026-10-17T19:00:00Z')).lastrowid
        db.execute('''INSERT INTO news(news_id,title,url,url_key,title_key,story_key,source,
          published_utc,status,created_at,updated_at)
          VALUES ('main1','Title','https://example.org/news','newkey','titlekey','story','Source',
            '2026-10-17T21:00:00Z','analysed','2026-10-17T21:00:00Z','2026-10-17T21:00:00Z')''')
        db.execute('''INSERT INTO news_company_relations
            (relation_id,news_id,company_id,entity_key,entity_type,entity_name,status,
            created_at,updated_at)
            VALUES ('rel2','main1',?,'CIK:0000000009','company','Target Company',
                    'connected','2026-10-17T21:05:00Z','2026-10-17T21:05:00Z')''',(company,))
    r=quality_counts(db,NOW)
    assert r['checks']['parsed_without_items']==1
    assert r['checks']['links_missing_reason']==1
    assert r['checks']['inconsistent_news_analysis']==1
    assert r['checks']['main_without_links']==0
    # Render uses only data, no disk writes.
    try:
        from dash import html
    except ImportError:
        pass
    else:
        assert render_quality(r)
    db.close()


def test_data_quality_sql_has_no_writes():
    from pathlib import Path
    from dashboard import data_quality
    contents=Path(data_quality.__file__).read_text()
    assert all(fragment not in contents for fragment in ('ALTER TABLE','UPDATE items SET', 'DELETE FROM','INSERT INTO'))
