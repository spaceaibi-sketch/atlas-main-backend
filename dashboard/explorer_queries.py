"""Read-only, server-paginated dashboard queries. All user filters are bound SQL parameters."""
from __future__ import annotations

PAGE_SIZE = 24


def _page(page):
    try:
        return max(1, min(int(page or 1), 1000000))
    except (ValueError, TypeError):
        return 1


def _find(search):
    """Escape LIKE metacharacters; user-entered searches are literal substrings."""
    s = (search or '').strip()[:150]
    return '%' + s.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'


def _fetch(db, count_sql, list_sql, params, page=1):
    count = int(db.execute(count_sql, params).fetchone()[0])
    page = min(_page(page), max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE))
    rows = [dict(x) for x in db.execute(list_sql + ' LIMIT ? OFFSET ?', (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))]
    return rows, count, page


def companies(db, search='', order='name', page=1):
    where = "WHERE (c.name LIKE ? ESCAPE '\\' OR COALESCE(c.ticker,'') LIKE ? ESCAPE '\\' OR c.cik LIKE ? ESCAPE '\\')" if search else ''
    params = (_find(search),)*3 if search else ()
    sort = {
        'name': 'c.name COLLATE NOCASE, c.id',
        'filings': 'last_filing DESC, c.name COLLATE NOCASE',
        'news': 'last_news DESC, c.name COLLATE NOCASE',
        'impact': 'c.acumm_impact DESC, c.name COLLATE NOCASE',
        'monetary': 'c.acumm_monetary DESC, c.name COLLATE NOCASE',
        'confidence': 'c.accum_confdent DESC, c.name COLLATE NOCASE',
    }.get(order, 'c.name COLLATE NOCASE, c.id')
    sql = f'''SELECT c.id,c.cik,c.name,c.acumm_impact,c.acumm_monetary,c.accum_confdent,COALESCE(c.ticker,'') ticker,
      COALESCE(c.exchange,'') exchange,
      (SELECT COUNT(*) FROM atlas_filings f WHERE f.company_id=c.id) filings,
      (SELECT MAX(filed_date) FROM atlas_filings f WHERE f.company_id=c.id) last_filing,
      (SELECT COUNT(*) FROM atlas_links r WHERE r.company_id=c.id) news_count,
      (SELECT MAX(n.published_utc) FROM atlas_articles n
         JOIN atlas_links r ON r.news_id=n.news_id WHERE r.company_id=c.id) last_news
      FROM companies c {where} ORDER BY {sort}'''
    return _fetch(db, f'SELECT COUNT(*) FROM companies c {where}', sql, params, page)


def filings(db, search='', kind='all', status='all', page=1):
    cond=[]; args=[]
    if search:
        cond.append("(c.name LIKE ? ESCAPE '\\' OR c.cik LIKE ? ESCAPE '\\' OR COALESCE(c.ticker,'') LIKE ? ESCAPE '\\')")
        args.extend([_find(search)]*3)
    if kind in ('8-K','10-K','10-Q'):
        cond.append('f.form_type=?');args.append(kind)
    if status in ('created','skipped','connected','analysed','extracted','parsed','error'):
        cond.append('f.status=?');args.append(status)
    where='WHERE '+' AND '.join(cond) if cond else ''
    frm=f'FROM atlas_filings f JOIN companies c ON c.id=f.company_id {where}'
    sql=f'''SELECT f.id,f.form_type,f.filed_date,f.status,c.name company_name,c.ticker,
      (SELECT COUNT(*) FROM atlas_items i WHERE i.form_id=f.id) item_count
      {frm} ORDER BY f.filed_date DESC,f.id DESC'''
    return _fetch(db,'SELECT COUNT(*) '+frm,sql,tuple(args),page)


def items(db, search='', status='all', page=1):
    cond=[];args=[]
    if search:
        cond.append("(c.name LIKE ? ESCAPE '\\' OR COALESCE(c.ticker,'') LIKE ? ESCAPE '\\' OR i.item_name LIKE ? ESCAPE '\\' OR i.item_type LIKE ? ESCAPE '\\')")
        args.extend([_find(search)]*4)
    if status in ('created','skipped','connected','analysed','processed','rejected','error'):
        cond.append('i.status=?');args.append(status)
    where='WHERE '+' AND '.join(cond) if cond else ''
    frm='FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id JOIN companies c ON c.id=f.company_id '+where
    sql=f'''SELECT i.id,i.form_id,i.item_type,i.item_name,i.status,i.created_at,
      f.form_type,c.name company_name,c.ticker {frm} ORDER BY i.created_at DESC,i.id DESC'''
    return _fetch(db,'SELECT COUNT(*) '+frm,sql,tuple(args),page)


def news(db, search='', status='all', page=1):
    cond=[];args=[]
    if search:
        cond.append("(n.title LIKE ? ESCAPE '\\' OR n.source LIKE ? ESCAPE '\\')")
        args.extend([_find(search)]*2)
    if status in ('created','skipped','connected','analysed'):
        cond.append('n.status=?');args.append(status)
    where='WHERE '+' AND '.join(cond) if cond else ''
    sql=f'''SELECT n.news_id,n.title,n.source,n.published_utc,n.status,
      (SELECT COUNT(*) FROM atlas_links r WHERE r.news_id=n.news_id) links
      FROM atlas_articles n {where} ORDER BY n.published_utc DESC,n.created_at DESC,n.news_id'''
    return _fetch(db,f'SELECT COUNT(*) FROM atlas_articles n {where}',sql,tuple(args),page)


def company_detail(db, cik):
    row=db.execute('SELECT * FROM companies WHERE cik=?',(str(cik),)).fetchone()
    if row is None: return None, [], []
    company=dict(row)
    forms=[dict(x) for x in db.execute('''SELECT id,form_type,filed_date,status FROM atlas_filings
        WHERE company_id=? ORDER BY filed_date DESC LIMIT 8''',(company['id'],))]
    articles=[dict(x) for x in db.execute('''SELECT n.news_id,n.title,n.published_utc,r.correlation_reason,r.status
      FROM atlas_links r JOIN atlas_articles n ON n.news_id=r.news_id
      WHERE r.company_id=? ORDER BY n.published_utc DESC LIMIT 8''',(company['id'],))]
    return company,forms,articles


def filing_detail(db, form_id):
    row=db.execute('''SELECT f.*,c.cik,c.name company_name,c.ticker FROM atlas_filings f
        JOIN companies c ON c.id=f.company_id WHERE f.id=?''',(form_id,)).fetchone()
    if row is None:return None,[]
    items=[dict(x) for x in db.execute('''SELECT id,item_type,item_name,status,created_at FROM atlas_items
        WHERE form_id=? ORDER BY item_type,item_occurrence''',(form_id,))]
    return dict(row),items


def item_detail(db, item_id):
    row=db.execute('''SELECT i.*,f.form_type,f.filed_date,f.index_url,f.primary_url,f.file_guid form_guid,
        c.name company_name,c.cik,c.ticker FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id
        JOIN companies c ON c.id=f.company_id WHERE i.id=?''',(item_id,)).fetchone()
    return dict(row) if row else None


def news_detail(db, news_id):
    row=db.execute('SELECT * FROM atlas_articles WHERE news_id=?',(news_id,)).fetchone()
    if row is None:return None,[]
    relations=[dict(x) for x in db.execute('''SELECT r.*,c.cik,c.ticker FROM atlas_links r
       LEFT JOIN companies c ON c.id=r.company_id WHERE r.news_id=?
       ORDER BY r.entity_name COLLATE NOCASE''',(news_id,))]
    return dict(row),relations


def recent_activity(db):
    news_rows=[dict(x) for x in db.execute('''SELECT news_id,title,published_utc,status FROM atlas_articles
       ORDER BY published_utc DESC LIMIT 6''')]
    form_rows=[dict(x) for x in db.execute('''SELECT f.id,f.form_type,f.filed_date,c.name company_name
       FROM atlas_filings f JOIN companies c ON c.id=f.company_id ORDER BY f.filed_date DESC LIMIT 6''')]
    item_rows=[dict(x) for x in db.execute('''SELECT i.id,i.item_name,i.item_type,i.created_at,c.name company_name
       FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id JOIN companies c ON c.id=f.company_id
       ORDER BY i.created_at DESC LIMIT 6''')]
    return news_rows,form_rows,item_rows
