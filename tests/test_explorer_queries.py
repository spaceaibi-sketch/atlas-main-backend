"""Web redesign: SQL pagination, search, detail linkage, no schema migration."""
from datetime import datetime, timezone

from dashboard import explorer_queries as q
from dashboard.compact_summary import summary_counts
from news_pipeline.store import add_story, ensure_schema
from news_pipeline.feeds import Story
from sec_edgar.database import connect, upsert_company


def fixture_db(tmp_path):
    db=connect(tmp_path/'atlas.db');ensure_schema(db)
    with db:
        a=upsert_company(db,'0000012345','Embraer S.A.','EMBJ')
        b=upsert_company(db,'0000012346','NEWS CORP','NWS')
        form=db.execute('''INSERT INTO forms
            (company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
            VALUES (?,?,?,?,?,'parsed',?,?)''',
            (a,'8-K','Current report','2026-10-17T18:10:00Z',
             'https://www.sec.gov/Archives/test1','2026-10-17T18:12:00Z','2026-10-17T18:12:00Z')).lastrowid
        item=db.execute('''INSERT INTO items
            (form_id,item_type,item_name,item_text,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)''',
            (form,'8.01','Other events','Company announces a new program','processed',
             '2026-10-17T18:15:00Z','2026-10-17T18:15:00Z')).lastrowid
    story=Story('Embraer receives new contract','https://example.org/story-one',
                'Embraer S.A. confirms the contract','2026-10-17T18:30:00Z','Publisher','US','Economy')
    news_id=add_story(db,story)[1]
    with db:
        db.execute("UPDATE news SET status='connected' WHERE news_id=?",(news_id,))
        db.execute('''INSERT INTO news_company_relations
             (relation_id,news_id,company_id,entity_key,entity_type,entity_name,status,
              correlation_reason,created_at,updated_at)
             VALUES ('r1',?,?,?,'company','Embraer S.A.','connected',?,?,?)''',
             (news_id,a,'CIK:0000012345','Documented supply contract',
              '2026-10-17T18:31:00Z','2026-10-17T18:31:00Z'))
    return db,a,b,form,item,news_id


def test_all_companies_including_unlinked(tmp_path):
    db,*rest=fixture_db(tmp_path)
    rows,count,p=q.companies(db)
    assert count==2 and p==1 and {x['name'] for x in rows}=={'Embraer S.A.','NEWS CORP'}
    assert next(x for x in rows if x['name']=='NEWS CORP')['news_count']==0
    db.close()


def test_search_literal_and_paging(tmp_path):
    db,*rest=fixture_db(tmp_path)
    assert q.companies(db,'NWS')[1]==1
    assert q.companies(db,'news corp')[1]==1
    assert q.companies(db,'%')[1]==0 # % must not become wildcard
    assert q.companies(db,'_')[1]==0
    assert q.companies(db,'0000012345')[1]==1
    with db:
        for j in range(45):upsert_company(db,f'{j+222222:010d}',f'Test Company {j:02d}')
    r,c,p=q.companies(db,'Test Company',page=2)
    assert (c,p,len(r))==(45,2,21)
    db.close()


def test_filing_item_and_news_filters(tmp_path):
    db,a,b,form,item,news_id=fixture_db(tmp_path)
    assert q.filings(db,'EMBJ','8-K','parsed')[1]==1
    assert q.filings(db,'NEWS CORP')[1]==0
    assert q.items(db,'Embraer','processed')[1]==1
    assert q.items(db,status='analysed')[1]==0
    assert q.news(db,'contract','connected')[1]==1
    assert q.news(db,'prediction')[1]==0
    assert q.news(db,status='skipped')[1]==0
    db.close()


def test_detail_queries_and_reason(tmp_path):
    db,a,b,form,item,news_id=fixture_db(tmp_path)
    co,forms,articles=q.company_detail(db,'0000012345')
    assert co['name']=='Embraer S.A.' and forms[0]['id']==form
    assert articles[0]['correlation_reason']=='Documented supply contract'
    filing,sections=q.filing_detail(db,form)
    assert filing['status']=='parsed' and sections[0]['id']==item
    assert q.item_detail(db,item)['item_text']=='Company announces a new program'
    article,rels=q.news_detail(db,news_id)
    assert article['title']=='Embraer receives new contract'
    assert rels[0]['correlation_reason']=='Documented supply contract'
    assert q.news_detail(db,'missing')==(None,[])
    db.close()


def test_last_item_extracted_uses_created_at_not_ai_updated(tmp_path):
    db,a,b,form,item,news_id=fixture_db(tmp_path)
    with db:
        db.execute("UPDATE items SET status='analysed',updated_at='2026-10-17T20:00:00Z' WHERE id=?",(item,))
    s=summary_counts(db,now=datetime(2026,10,17,20,15,tzinfo=timezone.utc))
    assert s['last_item_extracted']=='2026-10-17T18:15:00Z'
    assert s['last_item_analysed']=='2026-10-17T20:00:00Z'
    db.close()


def test_queries_use_no_mutating_statements():
    from pathlib import Path
    text=Path(q.__file__).read_text()
    for keyword in ('INSERT INTO','ALTER TABLE','UPDATE forms','UPDATE news','DELETE FROM'):
        assert keyword not in text
