"""News-specific dashboard queries and presentation."""
from __future__ import annotations
from .ai_cards import metric_card


def news_entities(db,search=''):
    rows=db.execute('''SELECT entity_key cik, MAX(entity_name) name, '' ticker,
           COUNT(*) num_forms, MAX(n.published_utc) latest
           FROM atlas_links r JOIN atlas_articles n ON n.news_id=r.news_id
           WHERE r.entity_type IN ('government','industry')
           GROUP BY entity_key ORDER BY latest DESC''').fetchall()
    return [dict(r) for r in rows if not search or search.casefold() in r['name'].casefold() or search.casefold() in r['cik'].casefold()]


def list_company_news(db,selector):
    if not selector:return []
    clause='r.entity_key=?' if selector.startswith(('GOV:','INDUSTRY:')) else 'r.company_id=(SELECT id FROM companies WHERE cik=?)'
    sql='''SELECT r.relation_id,n.news_id,n.title,n.published_utc,n.source,n.status,
              r.ai_impact,r.ai_confidence,r.entity_name,r.correlation_reason
        FROM atlas_links r JOIN atlas_articles n ON n.news_id=r.news_id WHERE '''+clause+'''
        ORDER BY n.published_utc DESC,n.created_at DESC LIMIT 750'''
    return [dict(r) for r in db.execute(sql,(selector,))]


def load_news_pair(db,relation_id,selector):
    if not relation_id or not selector:return None
    clause='r.entity_key=?' if selector.startswith(('GOV:','INDUSTRY:')) else 'r.company_id=(SELECT id FROM companies WHERE cik=?)'
    row=db.execute('''SELECT r.*, n.title, n.url, n.summary, n.source,
        n.published_utc, n.article_text, n.status AS news_status
        FROM atlas_links r JOIN atlas_articles n ON n.news_id=r.news_id
        WHERE r.relation_id=? AND '''+clause,(relation_id,selector)).fetchone()
    if not row:
        return None
    return dict(row)


def metric(label,value,reason,heading,positive=False,negative=False):
    tone='score-positive' if positive else 'score-negative' if negative else ''
    return metric_card(label,value,heading,reason,tone=tone)


def display_news_pair(r):
    """Show stored news content immediately, with metadata and optional AI results.

    RSS-only feeds may provide summaries rather than full article text. The UI
    must not describe a feed summary as the full article.
    """
    from dash import html
    if not r:
        return html.P('Choose a linked news headline in the left navigation.', className='empty')
    score = r['ai_impact']
    confidence = r['ai_confidence']
    full_text = (r.get('article_text') or '').strip()
    summary = (r.get('summary') or '').strip()
    news_text = full_text or summary or (r['title'] or '')
    text_label = 'Article text' if full_text else 'Article summary' if summary else 'Headline'
    reason = (r.get('correlation_reason') or '').strip()
    children = [
        html.H3(r['title'], className='news-title'),
        html.Div(className='form-meta', children=[
            html.Span(r.get('source') or 'Unknown source'),
            html.Span((r.get('published_utc') or '')[:16].replace('T', ' ') + ' UTC'
                      if r.get('published_utc') else ''),
        ]),
        html.Div(className='links', children=[html.A(
            'Read original', href=r['url'], target='_blank',
            rel='noopener noreferrer')]),
        html.Div(className='news-correlation', children=[
            html.Strong(r['entity_name']),
            html.P(reason or 'No matching reason recorded'),
        ]),
        html.Section(className='news-content', children=[
            html.Div(text_label, className='news-content-head'),
            html.Pre(news_text, className='news-article-body'),
        ]),
    ]
    if r['news_status'] == 'analysed':
        children.extend([
            html.Div('AI assessment', className='section-label'),
            html.Div(className='assessment-grid', children=[
                metric('Impact · −10 to +10', f'{score:+d}',
                       r['ai_reason'], 'Impact reasoning', score > 0, score < 0),
                metric('Confidence · 0 to 10', f'{confidence:.1f}/10',
                       r['ai_evidence'], 'Supporting evidence'),
                metric('Monetary effect · USD', f"${r['ai_monetary_effect']:,.2f}",
                       r['ai_monetary_effect_reason'], 'Monetary explanation'),
                metric('Speculative 1-week price scenario',
                       f"{r['price_change_1w_pct']:+.2f}%",
                       r['price_change_reason'], 'Scenario explanation'),
            ]),
        ])
    else:
        children.append(html.P('AI assessment not completed', className='analysis-placeholder'))
    return html.Div(children=children)
