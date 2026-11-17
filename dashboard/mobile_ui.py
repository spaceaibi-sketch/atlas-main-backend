"""Android-inspired company workspace as native Dash components.

This page is additive: the existing Data Quality component and callbacks remain
untouched. Browser favorites/theme are local preferences, not server writes.
"""
from __future__ import annotations

from contextlib import closing
from urllib.parse import urlencode, parse_qs

from dash import Input, Output, State, dcc, html, no_update

from dashboard import mobile_queries as q
from dashboard.company_analytics import company_assessments


def href(company_id=None, tab='overview', **params):
    arguments = dict(company=company_id, tab=tab) if company_id else {}
    arguments.update({k:v for k,v in params.items() if v is not None})
    return '/mobile' + ('?' + urlencode(arguments) if arguments else '')


def args_from_search(value):
    args = parse_qs((value or '').lstrip('?'))
    cid = args.get('company',[''])[0]
    company_id = int(cid) if cid.isdecimal() and len(cid) <= 16 else None
    tab = args.get('tab',['overview'])[0]
    if tab not in {'overview','news','items','forms'}:
        tab = 'overview'
    return company_id,tab,args


def node(label, value='', css=''):
    return html.Div(className='mobile-stat '+css,children=[html.Span(label),html.Strong(value)])


def fmt_num(value, unit='', digits=2):
    if value is None:
        return '—'
    return f'{float(value):+,.{digits}f}{unit}'


def fmt_date(value):
    return str(value).replace('T',' ')[:16]+' UTC' if value else '—'


def metric(label, value, explanation='', color=''):
    return html.Div(className='mobile-metric '+color,children=[
        html.Span(label),html.Strong(value),
        html.P(explanation or 'No supporting evidence recorded',className='mobile-metric-info')])


def score_tiles(row):
    completed = row['status']=='analysed'
    impact = row['ai_impact'] if completed else None
    return html.Div(className='mobile-metrics', children=[
        metric('Impact',fmt_num(impact,' / 10',0) if completed else '—',row.get('ai_reason'),
               'positive' if impact is not None and impact>0 else 'negative' if impact is not None and impact<0 else ''),
        metric('Confidence',f"{row['ai_confidence']:.1f} / 10" if completed and row['ai_confidence'] is not None else '—',row.get('ai_evidence')),
        metric('Monetary effect',fmt_num(row['ai_monetary_effect'],' USD') if completed else '—',row.get('ai_monetary_effect_reason')),
        metric('7-day scenario',fmt_num(row['price_change_1w_pct'],'%') if completed else '—',row.get('price_change_reason')),
    ])


def mobile_layout():
    return html.Section(id='page-mobile',className='workspace-page atlas-mobile',children=[
        dcc.Store(id='mobile-favorites',storage_type='local',data=[]),
        dcc.Store(id='mobile-theme',storage_type='local',data='light'),
        html.Div(className='mobile-topline',children=[
            html.Div(children=[html.Span('ATLAS',className='mobile-brand'),
                      html.Span('Company intelligence',className='mobile-subtitle')]),
            html.Div(className='mobile-top-actions',children=[
                dcc.Link('Directory',href='/mobile',className='mobile-small-link'),
                html.Button('◐ Theme',id='mobile-theme-button',n_clicks=0,className='mobile-icon-button'),
                html.Button('☆ Favorite',id='mobile-fav-toggle',n_clicks=0,
                            className='mobile-icon-button',style={'display':'none'}),
            ])]),
        html.Div(id='mobile-controls',className='mobile-controls',children=[
            dcc.Input(id='mobile-search',type='search',placeholder='Search company, ticker or CIK',
                      debounce=True,className='mobile-search'),
            dcc.Dropdown(id='mobile-sort',value='recent',clearable=False,
                options=[{'label':'Latest activity','value':'recent'},
                         {'label':'7-day impact','value':'impact'},
                         {'label':'7-day monetary','value':'monetary'},
                         {'label':'Company name','value':'name'}],className='mobile-dropdown'),
            dcc.RadioItems(id='mobile-watchlist-mode',value='all',options=[
                {'label':'All companies','value':'all'}, {'label':'☆ Watchlist','value':'favorites'}],
                inline=True,className='mobile-radio'),
            html.Label('Page',htmlFor='mobile-page-number'),
            dcc.Input(id='mobile-page-number',type='number',min=1,step=1,value=1,
                      className='mobile-page-number'),
        ]),
        dcc.Loading(type='dot',children=html.Div(id='mobile-body',className='mobile-body')),
    ])


def directory_cards(db,search,sort,page,favorites,watchlist):
    rows,total,page = q.directory(db,search,sort,page,favorites,watchlist=='favorites')
    favorites = {int(x) for x in (favorites or []) if str(x).isdecimal()}
    return [html.Div(className='mobile-screen-head',children=[
        html.P('RESEARCH DIRECTORY',className='mobile-eyebrow'),html.H1('Companies'),
        html.P(f'{total:,} companies · Page {page} · Open a company for research and SEC records')]),
        html.Div(className='mobile-directory',children=[
            dcc.Link(href=href(row['id']),className='mobile-company-card',children=[
                html.Div(className='mobile-avatar',children=(row['name'] or '?')[0].upper()),
                html.Div(className='mobile-company-main',children=[
                    html.Div(className='mobile-company-title',children=[
                        html.Strong(row['ticker'] or row['name']),
                        html.Span('★' if row['id'] in favorites else '',className='mobile-star')]),
                    html.P(row['name']),html.Small(row['exchange'] or f"CIK {row['cik']}"),
                    html.Div(className='mobile-company-foot',children=[
                        html.Span('Impact '+fmt_num(row['acumm_impact'],digits=2)),
                        html.Span('Last $'+f"{row['last_price']:,.2f}" if row['last_price'] is not None else 'Price —'),
                        html.Span(f"SEC {row['filings']} · News {row['news_count']}")])]),
                html.Span('›',className='mobile-chevron'),
            ]) for row in rows] or [html.Div('No companies match these filters.',className='mobile-empty')]),
        html.P('Use the Page control to browse additional companies.',className='mobile-footnote') if total>q.PAGE_SIZE else html.Span()]


def info_card(label,value):
    return html.Div(className='mobile-info-field',children=[html.Span(label),html.Strong(str(value) if value else '—')])


def tabs(company_id,active):
    return html.Nav(className='mobile-tabs',children=[
        dcc.Link(label,href=href(company_id,tab=tab),className='mobile-tab'+(' active' if tab==active else ''))
        for tab,label in [('overview','Overview'),('news','News'),('items','SEC Items'),('forms','SEC Forms')]])


def detail_view(kind,row,cid,tab,page=1,sort='time'):
    is_news=kind=='news'
    name=row['title'] if is_news else f"Item {row['item_type']} · {row['item_name']}"
    source_url=row['url'] if is_news else row['primary_url'] or row['index_url']
    body=(row.get('article_text') or row.get('summary') or row['title']) if is_news else row['item_text']
    return html.Section(className='mobile-panel mobile-detail',children=[
        dcc.Link('← Back to '+('News' if is_news else 'SEC Items'),href=href(cid,tab,p=page,sort=sort),className='mobile-back'),
        html.H2(name),html.P(fmt_date(row['event_at'])),
        html.Div(className='mobile-chip',children=row['status']),
        html.H3('AI assessment'),score_tiles(row),
        html.H3('Company correlation') if is_news else html.H3('Source filing'),
        html.P(row.get('correlation_reason') or 'No recorded reason') if is_news else
            dcc.Link('Open filing record',href=f"/filings?form={row['form_id']}"),
        html.A('Original source ↗',href=source_url,target='_blank',rel='noopener noreferrer') if source_url else html.Span(),
        html.H3('Original article' if is_news else 'Original Item text'),
        html.Pre(body or 'Text unavailable',className='mobile-source-text'),
        html.P('Processing: '+row['errors'],className='mobile-error') if row.get('errors') else html.Span(),
    ])


def preview_card(row, cid, tab, kind, page=None, sort=None):
    is_news=kind=='news'
    title=row['title'] if is_news else f"Item {row['item_type']} · {row['item_name']}"
    subtitle=(row['publisher'] if is_news else row['form_type']) or ''
    return dcc.Link(href=href(cid,tab,record=row['id'],p=page,sort=sort),className='mobile-list-card',children=[
        html.Div(className='mobile-list-meta',children=[html.Span(fmt_date(row['event_at'])),html.Span(subtitle)]),
        html.H3(title),
        html.Div(className='mobile-compact-scores',children=[
            html.Span('Impact '+fmt_num(row['ai_impact'],digits=0) if row['status']=='analysed' else 'Not analysed'),
            html.Span('Confidence '+fmt_num(row['ai_confidence']) if row['status']=='analysed' else ''),
            html.Span('USD '+fmt_num(row['ai_monetary_effect']) if row['status']=='analysed' else ''),
            html.Span('7d '+fmt_num(row['price_change_1w_pct'],'%') if row['status']=='analysed' else '')]),
        html.Small('Read original text and assessment →',className='mobile-card-open'),
    ])


def chart_figure(bars,evidence):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig=make_subplots(specs=[[{'secondary_y': True}]])
    if bars:
        fig.add_trace(go.Scatter(x=[b['bar_time_utc'] for b in bars],y=[b['close'] for b in bars],
            mode='lines',name='Observed 15-min close',line={'width':1.5},hovertemplate='%{x}<extra>Close: $%{y:.2f}</extra>'),secondary_y=False)
    for kind,name,color,symbol in [('news','Scored news','#4288df','circle'),('item','Scored SEC Items','#e5a53e','diamond')]:
        rows=[r for r in evidence if r['source_type']==kind]
        if rows:
            fig.add_trace(go.Scatter(x=[r['event_time'] for r in rows],y=[r['impact'] for r in rows],
                customdata=[r['title'] for r in rows],mode='markers',name=name,
                marker={'color':color,'symbol':symbol,'size':10},
                hovertemplate='%{x}<br>%{customdata}<br>AI impact: %{y}/10<extra></extra>'),secondary_y=True)
    fig.update_layout(template='plotly_white',height=365,margin={'l':45,'r':40,'t':20,'b':35},
                      legend={'orientation':'h','y':1.14},hovermode='closest')
    fig.update_yaxes(title_text='Observed price (USD)',secondary_y=False)
    fig.update_yaxes(title_text='AI impact (−10 to +10)',range=[-10,10],secondary_y=True)
    fig.update_xaxes(rangeslider={'visible':True,'thickness':0.08})
    return fig


def overview(db,c):
    cid=c['id']
    summary=q.assessment_summary(db,cid)
    evidence=company_assessments(db,cid)
    bars=q.price_bars(db,cid)
    top_news=q.related_news(db,cid,limit=1)
    top_items=q.related_items(db,cid,limit=1)
    return [html.Div(className='mobile-summary-grid',children=[
        node('7-day cumulative impact',fmt_num(c['acumm_impact'])),
        node('7-day monetary',fmt_num(c['acumm_monetary'],' USD')),
        node('7-day confidence',fmt_num(c['accum_confdent'],' / 10')),
        node('Last observed price',f"${c['last_price']:,.2f}" if c['last_price'] is not None else '—'),
        node('Scored news',str(summary['scored_news'])),node('Scored SEC Items',str(summary['scored_items'])),
    ]),
    html.Div(className='mobile-panel',children=[html.H2('Latest assessments'),
        html.P('Last news: '+fmt_date(summary['last_news'])+' · Last Item: '+fmt_date(summary['last_item'])),
        html.H3('News'),preview_card(top_news[0],cid,'news','news') if top_news else html.P('No linked news'),
        html.H3('SEC Item'),preview_card(top_items[0],cid,'items','item') if top_items else html.P('No Items')]),
    html.Div(className='mobile-panel',children=[html.H2('Observed price & assessment timeline'),
        html.P('Prices are stored Alpaca 15-minute closes only. Markers are completed AI assessments from the last 7 days, shown against a separate impact axis. No inferred prices.'),
        dcc.Graph(figure=chart_figure(bars,evidence),config={'displaylogo':False},className='mobile-chart'),
        html.Small(f'{len(bars):,} stored price bars · Last price: '+fmt_date(c['last_price_at']))]),
    html.Div(className='mobile-panel',children=[html.H2('Company profile'),
        info_card('Name',c['name']),info_card('Ticker',c['ticker']),
        info_card('Exchange',c['exchange']),info_card('CIK',c['cik']),
        html.P('The company description is not present in this backend database.',className='mobile-footnote')])]


def mobile_company(db,c,tab,params,sort):
    cid=c['id']
    result=[dcc.Link('← All companies',href='/mobile',className='mobile-back'),
      html.Div(className='mobile-selected-head',children=[
          html.Div(className='mobile-avatar',children=(c['name'] or '?')[0].upper()),
          html.Div(children=[html.P('COMPANY WORKSPACE',className='mobile-eyebrow'),
                             html.H1(c['ticker'] or c['name']),html.Strong(c['name']),html.P(c['exchange'] or '')])]),
      tabs(cid,tab)]
    record = params.get('record',[''])[0]
    if tab=='overview':
        return result+overview(db,c)
    if tab in ('news','items'):
        sort = sort if sort in ('time','abs_impact','abs_monetary') else 'time'
        total=q.company_record_count(db,cid,tab)
        try:
            requested=max(1,int(params.get('p',['1'])[0]))
        except (TypeError,ValueError):
            requested=1
        pages=max(1,(total+39)//40)
        page=min(requested,pages)
        rows=q.related_news(db,cid,sort,limit=40,offset=(page-1)*40) if tab=='news' else q.related_items(db,cid,sort,limit=40,offset=(page-1)*40)
        result.append(html.Div(className='mobile-panel',children=[
            html.Div(className='mobile-section-heading',children=[html.H2('News' if tab=='news' else 'SEC Items'),
                dcc.Link('Search all '+('news' if tab=='news' else 'items')+' ↗',href='/news' if tab=='news' else '/items')]),
            html.P(f'{total:,} records · Page {page} of {pages}'),
            dcc.Link('Sort by time',href=href(cid,tab,sort='time'),className='mobile-sort-link'),
            dcc.Link('Absolute impact',href=href(cid,tab,sort='abs_impact'),className='mobile-sort-link'),
            dcc.Link('Absolute monetary',href=href(cid,tab,sort='abs_monetary'),className='mobile-sort-link'),
        ]))
        record=params.get('record',[''])[0]
        chosen=next((r for r in rows if str(r['id'])==record),None) if record else None
        if chosen:
            result.append(detail_view('news' if tab=='news' else 'item',chosen,cid,tab,page,sort))
        else:
            result.append(html.Div(className='mobile-cards',children=[preview_card(r,cid,tab,'news' if tab=='news' else 'item',page,sort) for r in rows] or [html.P('No related records')]))
        result.append(html.Div(className='mobile-list-pager',children=[
            dcc.Link('← Newer',href=href(cid,tab,p=page-1,sort=sort)) if page>1 else html.Span(),
            html.Span(f'{page} / {pages}'),
            dcc.Link('Older →',href=href(cid,tab,p=page+1,sort=sort)) if page<pages else html.Span(),
        ]))
        return result
    forms=q.related_forms(db,cid,limit=500)
    form_id=params.get('form',[''])[0]
    selected=next((f for f in forms if str(f['id'])==form_id),None)
    result.append(html.Div(className='mobile-panel',children=[html.H2('SEC Forms'),html.P(f'{len(forms)} filings')]))
    if selected:
        result.append(html.Div(className='mobile-panel',children=[
            dcc.Link('← All SEC Forms',href=href(cid,'forms'),className='mobile-back'),
            html.H2(f"{selected['form_type']} · {fmt_date(selected['filed_date'])}"),
            html.P(f"Status: {selected['status']} · Extracted Items: {selected['item_count']}"),
            html.A('SEC filing index ↗',href=selected['index_url'],target='_blank',rel='noopener noreferrer'),
            html.P('Error: '+selected['error'],className='mobile-error') if selected['error'] else html.Span(),
            html.H3('Items in this filing'),
            html.Div(className='mobile-cards',children=[preview_card(x,cid,'items','item') for x in q.related_items(db,cid,form_id=selected['id'],limit=500)] or [html.P('No extracted Items')])]))
    else:
        result.append(html.Div(className='mobile-cards',children=[
            dcc.Link(className='mobile-list-card',href=href(cid,'forms',form=f['id']),children=[
                html.H3(f"{f['form_type']} · {fmt_date(f['filed_date'])}"),
                html.P(f['form_name']),html.P(f"{f['item_count']} Items · {f['status']}"),
                html.Small('View filing and extracted Items →')]) for f in forms] or [html.P('No SEC filings available')]))
    return result


def register_mobile_callbacks(app,db_read):
    @app.callback(Output('mobile-favorites','data'),Input('mobile-fav-toggle','n_clicks'),
                  State('mobile-favorites','data'),State('route','search'),prevent_initial_call=True)
    def toggle_favorite(clicks, favorites, search):
        cid,_,_=args_from_search(search)
        if not clicks or not cid:
            return no_update
        existing={int(x) for x in (favorites or []) if str(x).isdecimal()}
        if cid in existing:existing.remove(cid)
        else:existing.add(cid)
        return sorted(existing)

    @app.callback(Output('mobile-theme','data'),Input('mobile-theme-button','n_clicks'),
                  State('mobile-theme','data'),prevent_initial_call=True)
    def toggle_theme(clicks,current):
        return ('light' if current=='dark' else 'dark') if clicks else no_update

    @app.callback(Output('page-mobile','className'),Input('mobile-theme','data'))
    def mobile_theme(theme):
        return 'workspace-page atlas-mobile'+(' mobile-dark' if theme=='dark' else '')

    @app.callback(Output('mobile-body','children'),Output('mobile-controls','style'),
                  Output('mobile-fav-toggle','style'),Output('mobile-fav-toggle','children'),
                  Input('route','pathname'),Input('route','search'),
                  Input('mobile-search','value'),Input('mobile-sort','value'),
                  Input('mobile-page-number','value'),Input('mobile-watchlist-mode','value'),
                  Input('mobile-favorites','data'),Input('refresh','n_intervals'))
    def render_mobile(path,search,query,order,page,watchlist,favorites,_tick):
        if path!='/mobile':
            return no_update,no_update,no_update,no_update
        cid,tab,params=args_from_search(search)
        with closing(db_read()) as db:
            if cid is None:
                return directory_cards(db,query,order,page,favorites,watchlist),{'display':'flex'},{'display':'none'},'☆ Favorite'
            record=q.company(db,cid)
            if record is None:
                return html.Div('Company not found. Please select one from the directory.',className='mobile-empty'),{'display':'none'},{'display':'none'},'☆ Favorite'
            fav = cid in {int(x) for x in (favorites or []) if str(x).isdecimal()}
            return mobile_company(db,record,tab,params,params.get('sort',['time'])[0]),{'display':'none'},{'display':'inline-flex'},'★ Watchlisted' if fav else '☆ Add favorite'
