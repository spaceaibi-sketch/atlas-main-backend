"""Company list with independent SEC filing and linked-news recency sorting."""
from __future__ import annotations

SORTS = ('sec','news','name')


def company_rows(db,search='',sort_by='sec',limit=1500):
    if sort_by not in SORTS:
        sort_by='sec'
    order = {
        'sec': "COALESCE(f.latest,'') DESC, COALESCE(n.latest_news,'') DESC, c.name COLLATE NOCASE",
        'news': "COALESCE(n.latest_news,'') DESC, COALESCE(f.latest,'') DESC, c.name COLLATE NOCASE",
        'name': 'c.name COLLATE NOCASE ASC',
    }[sort_by]
    query='''SELECT c.cik,c.name,COALESCE(c.ticker,'') ticker,c.exchange,
            COALESCE(f.num_forms,0) num_forms,f.latest, n.latest_news,
            COALESCE(n.num_news,0) num_news
      FROM companies c
      LEFT JOIN (SELECT company_id,COUNT(*) num_forms,MAX(filed_date) latest
                 FROM atlas_filings GROUP BY company_id) f ON f.company_id=c.id
      LEFT JOIN (SELECT r.company_id,COUNT(*) num_news,MAX(a.published_utc) latest_news
                 FROM atlas_links r JOIN atlas_articles a ON a.news_id=r.news_id
                 WHERE r.company_id IS NOT NULL AND a.status IN ('connected','analysed')
                 GROUP BY r.company_id) n ON n.company_id=c.id
      WHERE (f.num_forms IS NOT NULL OR n.num_news IS NOT NULL)'''
    params=[]
    if search:
        query+=' AND (c.name LIKE ? OR c.cik LIKE ? OR c.ticker LIKE ?)'
        params.extend(['%'+search.strip()+'%']*3)
    query+=' ORDER BY '+order+' LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in db.execute(query,params)]
