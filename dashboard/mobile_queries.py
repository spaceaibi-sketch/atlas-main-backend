"""Company-scoped, read-only queries for the Android-inspired Dash workspace.

All identifiers and filters are SQL parameters, with explicit limits. Only
stored price bars and completed AI assessments are displayed as measurements.
"""
from __future__ import annotations

PAGE_SIZE = 24


def _like(value):
    return '%' + (value or '').strip()[:120].replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'


def directory(db, search='', order='recent', page=1, favorites=None, only_favorites=False):
    conditions = ["(c.name LIKE ? ESCAPE '\\' OR COALESCE(c.ticker,'') LIKE ? ESCAPE '\\' OR c.cik LIKE ? ESCAPE '\\')"] if search else []
    args = [_like(search)] * 3 if search else []
    if only_favorites:
        ids = sorted({int(x) for x in (favorites or []) if str(x).isdecimal() and int(x) > 0})[:2000]
        if not ids:
            return [], 0, 1
        conditions.append('c.id IN (' + ','.join('?' for _ in ids) + ')')
        args.extend(ids)
    where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    sort = {
        'recent': 'last_activity DESC, c.name COLLATE NOCASE',
        'impact': 'c.acumm_impact DESC, c.name COLLATE NOCASE',
        'monetary': 'ABS(c.acumm_monetary) DESC, c.name COLLATE NOCASE',
        'name': 'c.name COLLATE NOCASE, c.id',
    }.get(order, 'last_activity DESC, c.name COLLATE NOCASE')
    base = ''' FROM companies c '''
    total = db.execute('SELECT COUNT(*) ' + base + where, args).fetchone()[0]
    try:
        requested = max(1, int(page or 1))
    except (TypeError, ValueError):
        requested = 1
    current = min(requested, max(1, (total + PAGE_SIZE - 1)//PAGE_SIZE))
    sql = f'''SELECT c.id,c.cik,c.name,c.ticker,c.exchange,c.acumm_impact,c.acumm_monetary,
      c.accum_confdent, (SELECT MAX(f.filed_date) FROM atlas_filings f WHERE f.company_id=c.id) last_filing,
      (SELECT MAX(n.published_utc) FROM atlas_articles n JOIN atlas_links r ON r.news_id=n.news_id
       WHERE r.company_id=c.id) last_news,
      MAX(COALESCE((SELECT MAX(f.filed_date) FROM atlas_filings f WHERE f.company_id=c.id),''),
          COALESCE((SELECT MAX(n.published_utc) FROM atlas_articles n JOIN atlas_links r ON r.news_id=n.news_id
                    WHERE r.company_id=c.id),'')) last_activity,
      (SELECT COUNT(*) FROM atlas_filings f WHERE f.company_id=c.id) filings,
      (SELECT COUNT(*) FROM atlas_links r WHERE r.company_id=c.id) news_count,
      (SELECT p.close FROM price_bars p WHERE p.company_id=c.id ORDER BY p.bar_time_utc DESC LIMIT 1) last_price
      {base} {where} ORDER BY {sort} LIMIT ? OFFSET ?'''
    return [dict(x) for x in db.execute(sql, [*args, PAGE_SIZE, (current-1)*PAGE_SIZE])], total, current


def company(db, company_id):
    row = db.execute('''SELECT c.*,
        (SELECT close FROM price_bars WHERE company_id=c.id ORDER BY bar_time_utc DESC LIMIT 1) last_price,
        (SELECT bar_time_utc FROM price_bars WHERE company_id=c.id ORDER BY bar_time_utc DESC LIMIT 1) last_price_at
        FROM companies c WHERE c.id=?''', (company_id,)).fetchone()
    return dict(row) if row else None


def assessment_summary(db, company_id):
    counts = dict(db.execute('''SELECT
        (SELECT COUNT(*) FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id
         WHERE f.company_id=? AND i.status='analysed') AS scored_items,
        (SELECT COUNT(*) FROM atlas_links r JOIN atlas_articles n ON n.news_id=r.news_id
         WHERE r.company_id=? AND r.status='analysed' AND n.status IN ('connected','analysed')) AS scored_news''',
        (company_id,company_id)).fetchone())
    last = db.execute('''SELECT
      (SELECT MAX(i.updated_at) FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id
       WHERE f.company_id=? AND i.status='analysed') last_item,
      (SELECT MAX(r.updated_at) FROM atlas_links r
       WHERE r.company_id=? AND r.status='analysed') last_news''', (company_id,company_id)).fetchone()
    return {**counts, **dict(last)}


def related_news(db, company_id, order='time', limit=80, offset=0):
    sort = {
        'time': 'n.published_utc DESC, n.news_id',
        'abs_impact': 'ABS(r.ai_impact) DESC, n.published_utc DESC',
        'abs_monetary': 'ABS(r.ai_monetary_effect) DESC, n.published_utc DESC',
    }.get(order, 'n.published_utc DESC, n.news_id')
    return [dict(r) for r in db.execute(f'''SELECT n.news_id id,n.title,n.source publisher,n.url,n.summary,n.article_text,
      n.published_utc event_at,n.status news_status,'' AS batch_reason,
      r.relation_id,r.status,r.correlation_reason,r.ai_impact,r.ai_confidence,r.ai_reason,r.ai_evidence,
      r.ai_monetary_effect,r.ai_monetary_effect_reason,r.price_change_1w_pct,r.price_change_reason,r.errors
      FROM atlas_links r JOIN atlas_articles n ON n.news_id=r.news_id
      WHERE r.company_id=? ORDER BY {sort} LIMIT ? OFFSET ?''',(company_id,limit,offset))]


def related_items(db, company_id, order='time', limit=80, form_id=None, offset=0):
    sort = {
        'time': 'f.filed_date DESC,i.id DESC',
        'abs_impact': 'ABS(i.ai_impact) DESC,f.filed_date DESC',
        'abs_monetary': 'ABS(i.ai_monetary_effect) DESC,f.filed_date DESC',
    }.get(order, 'f.filed_date DESC,i.id DESC')
    predicate = ' AND f.id=?' if form_id is not None else ''
    args = (company_id,form_id,limit,offset) if form_id is not None else (company_id,limit,offset)
    return [dict(r) for r in db.execute(f'''SELECT i.id,i.form_id,i.item_type,i.item_name,i.item_text,i.status,i.errors,
       i.ai_impact,i.ai_confidence,i.ai_reason,i.ai_evidence,i.ai_monetary_effect,
       i.ai_monetary_effect_reason,i.price_change_1w_pct,i.price_change_reason,
       f.form_type,f.filed_date event_at,f.index_url,f.primary_url
       FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id
       WHERE f.company_id=? {predicate} ORDER BY {sort} LIMIT ? OFFSET ?''',args)]


def company_record_count(db, company_id, kind):
    if kind=='news':
        sql='SELECT COUNT(*) FROM atlas_links WHERE company_id=?'
    elif kind=='items':
        sql='SELECT COUNT(*) FROM atlas_items i JOIN atlas_filings f ON f.id=i.form_id WHERE f.company_id=?'
    else:
        raise ValueError('Unknown company record kind')
    return db.execute(sql,(company_id,)).fetchone()[0]


def related_forms(db, company_id, limit=80):
    return [dict(r) for r in db.execute('''SELECT f.id,f.form_type,f.form_name,f.filed_date,f.status,f.index_url,
         f.primary_url,f.file_guid,f.error,COUNT(i.id) AS item_count
         FROM atlas_filings f LEFT JOIN atlas_items i ON i.form_id=f.id WHERE f.company_id=?
         GROUP BY f.id ORDER BY f.filed_date DESC,f.id DESC LIMIT ?''',(company_id,limit))]


def price_bars(db, company_id, limit=10000):
    # Return oldest to newest for charting, but bound the query by newest N.
    return list(reversed([dict(r) for r in db.execute('''SELECT bar_time_utc,open,high,low,close,volume,feed
      FROM price_bars WHERE company_id=? ORDER BY bar_time_utc DESC LIMIT ?''',(company_id,limit))]))
