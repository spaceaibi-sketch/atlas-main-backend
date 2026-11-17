"""Atlas: focused, multi-page SEC filings, Items and company-news dashboard."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs, quote

from dash import Dash, Input, Output, State, dcc, html, no_update
from flask import abort, send_file

from ai.worker import brief_ai_error
from dashboard.compact_summary import summary_counts, render_summary
from dashboard.data_quality import quality_counts, render_quality
from dashboard.company_analytics import company_assessments
from dashboard.explorer_queries import (
    companies, filings, items, news, company_detail, filing_detail,
    item_detail, news_detail, recent_activity,
)
from sec_edgar.database import connect
from sec_edgar.reports import local_report_file
from sec_edgar.settings import database_path
from dashboard.mobile_ui import mobile_layout, register_mobile_callbacks
from dashboard.db_explorer_ui import db_explorer_layout, register_db_explorer_callbacks
from dashboard.unified_read_adapter import five_table_only
from dashboard.canonical_reader import CanonicalConnection
from dashboard.overview_status import record_status, latest_runs, render_overview
from shared.processing_log import log_step

DB_PATH = database_path()
# Never silently create an empty database or legacy schema at dashboard startup.
if not DB_PATH.is_file():
    raise RuntimeError(f'Atlas database not found: {DB_PATH}')
with closing(sqlite3.connect(f'{DB_PATH.as_uri()}?mode=ro', uri=True)) as _probe:
    if not five_table_only(_probe):
        raise RuntimeError('Atlas dashboard requires only five canonical tables; run offline migration first')
FIVE_TABLE_MODE = True

app = Dash(__name__, title='Atlas · Research workspace', suppress_callback_exceptions=True)
server = app.server


def db_read():
    db = sqlite3.connect(f'{DB_PATH.as_uri()}?mode=ro', uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    return CanonicalConnection(db)


@server.route('/reports/<guid>')
def report_route(guid):
    with closing(db_read()) as db:
        found = local_report_file(db, DB_PATH, guid)
    if found is None:
        abort(404)
    path, mime = found
    response = send_file(path, mimetype=mime, as_attachment=False,
                         download_name=path.name, conditional=True)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    )
    return response


PATHS = {'overview': '/', 'companies': '/companies', 'filings': '/filings',
         'items': '/items', 'news': '/news', 'quality': '/quality', 'mobile': '/mobile', 'database': '/database'}


def current_page(pathname):
    for name, path in PATHS.items():
        if pathname == path:
            return name
    return 'overview'


def query_id(search, key):
    """Never substitute URL parameters into SQL; IDs are always SQL-bound."""
    return parse_qs((search or '').lstrip('?')).get(key, [''])[0][:128]


def detail_url(page, key, value):
    return PATHS[page] + '?' + key + '=' + quote(str(value), safe='')


def stamp(value):
    if not value:
        return '—'
    return str(value).replace('T', ' ')[:16] + ' UTC'


def tag(value):
    # Show only the exact status values stored in the database.
    return html.Span(str(value), className='state state-' + str(value).lower())


def extlink(text, href):
    return html.A(text + ' ↗', href=href, target='_blank',
                  rel='noopener noreferrer', className='text-link') if href else html.Span()


def plain_link(label, page, key, value):
    return dcc.Link(label, href=detail_url(page,key,value), className='entry-link')


def nav_link(page,label):
    return dcc.Link(label, href=PATHS[page], id='nav-' + page, className='nav-link')


def search_input(id_, placeholder):
    return dcc.Input(id=id_, type='search', debounce=True, className='filter-search',
                     placeholder=placeholder, autoComplete='off')


def select(id_, choices):
    return dcc.Dropdown(id=id_, className='filter-select', clearable=False,
                        value='all' if 'all' in choices else choices[0],
                        options=[{'label':label,'value':value} for value,label in choices.items()])


def pager(page_name):
    return html.Div(className='pager', children=[
        html.Span(id=page_name+'-count', className='result-count'),
        html.Label('Page', htmlFor=page_name+'-page', className='page-label'),
        dcc.Input(id=page_name+'-page', type='number', min=1, step=1,
                  value=1, className='page-number'),
    ])


def table(headers, rows):
    if not rows:
        return html.Div('No matching records', className='empty-list')
    return html.Table(className='data-table', children=[
        html.Thead(html.Tr([html.Th(x, scope='col') for x in headers])),
        html.Tbody([html.Tr([html.Td(x) for x in r]) for r in rows]),
    ])


def workspace(page, title, subtitle, controls):
    return html.Section(id='page-'+page, className='workspace-page', children=[
        html.Div(className='page-intro', children=[html.Div([
            html.H1(title), html.P(subtitle)]),
        ]),
        html.Div(id=page+'-workspace', className='workspace-grid', children=[
            html.Section(className='surface listing', children=[
                html.Div(className='list-toolbar', children=controls),
                html.Div(id=page+'-table', className='table-scroll'),
                pager(page),
            ]),
            html.Aside(id=page+'-detail', className='record-pane', children=[
                html.Div(id=page+'-detail-content'),
                item_actions if page=='items' else html.Span(),
            ]),
        ]),
    ])


# Item actions stay mounted when record contents are refreshed.
item_actions=html.Div(id='item-action-bar',className='item-action-bar',children=[
    html.Button('Analyse selected Item',id='ai-analyse',n_clicks=0,className='primary-button'),
    dcc.Loading(type='dot',children=html.Div(id='ai-feedback',className='action-feedback')),
    html.Label('Ask AI about this Item',htmlFor='ai-question',className='input-label'),
    dcc.Textarea(id='ai-question',className='question-box',placeholder='Type a specific question…',rows=2),
    html.Button('Ask AI',id='ai-ask',n_clicks=0,className='secondary-button'),
    dcc.Loading(type='dot',children=html.Div(id='ai-answer',className='answer-box')),
])


def _overview_refresh_ms():
    try:
        configured = int(os.environ.get('ATLAS_DASH_OVERVIEW_REFRESH_MS', '10000'))
    except ValueError:
        configured = 10000
    return max(1000, min(60000, configured))


app.layout = html.Div(className='app-shell', children=[
    dcc.Location(id='route', refresh=False),
    dcc.Interval(id='refresh', interval=60000, n_intervals=0),
    dcc.Interval(id='ops-refresh', interval=_overview_refresh_ms(), n_intervals=0),
    dcc.Store(id='ai-revision', data=0),
    html.Header(className='app-header', children=[
        dcc.Link(className='wordmark', href='/mobile', children=[
            html.Span('A', className='brand-icon'), html.Span('ATLAS', className='brand-title')]),
        html.Nav(className='site-nav', **{'aria-label':'Main navigation'}, children=[
            nav_link('mobile','Atlas app'), nav_link('overview','Overview'), nav_link('companies','Companies'),
            nav_link('filings','SEC filings'), nav_link('items','Items'),
            nav_link('news','News'), nav_link('quality','Data quality'), nav_link('database','DB Explorer')]),
        html.Span('SEC research', className='app-strapline'),
    ]),
    html.Main(className='explorer', children=[
        mobile_layout(),
        db_explorer_layout(),
        html.Section(id='page-overview', className='workspace-page', children=[
            html.Div(className='page-intro', children=[html.Div([
                html.H1('Overview'), html.P('News and Items · live status and last completed pipeline runs')])]),
            html.Div(id='overview-operations', className='ops-overview'),
            html.H2('Recent activity',className='ops-activity-title'),
            html.Div(id='overview-feed', className='activity-grid'),
        ]),
        html.Section(id='page-quality', className='workspace-page quality-page', children=[
            html.Div(className='page-intro',children=[html.Div([
                html.H1('Data quality'), html.P('Processing status, recent activity and records needing attention')])]),
            html.Div(id='quality-summary',className='quality-summary'),
            html.Div(id='quality-content',className='quality-content'),
        ]),
        workspace('companies','Companies','Search all SEC companies and open their filings or related news', [
            search_input('companies-search','Search company, ticker or CIK'),
            dcc.Dropdown(id='companies-sort',value='name',clearable=False,
                options=[{'label':'Company name','value':'name'},
                         {'label':'Latest filing','value':'filings'},
                         {'label':'Latest linked news','value':'news'},
                         {'label':'7-day impact','value':'impact'},
                         {'label':'7-day monetary','value':'monetary'},
                         {'label':'7-day confidence','value':'confidence'}], className='filter-select')]),
        workspace('filings','SEC filings','Browse filing records, source reports and extracted sections', [
            search_input('filings-search','Search company, ticker or CIK'),
            select('filings-kind', {'all':'All types','8-K':'8-K','10-K':'10-K','10-Q':'10-Q'}),
            select('filings-status', {'all':'All statuses','created':'created','skipped':'skipped',
                                       'connected':'connected','analysed':'analysed'})]),
        workspace('items','Extracted Items','Review sections from SEC reports and run AI assessments', [
            search_input('items-search','Search company or Item'),
            select('items-status', {'all':'All statuses','created':'created','skipped':'skipped',
                                    'connected':'connected','analysed':'analysed'})]),
        workspace('news','News','Read articles and every stored company correlation reason', [
            search_input('news-search','Search headline or publisher'),
            select('news-status', {'all':'All statuses','created':'created','skipped':'skipped',
                                   'connected':'connected','analysed':'analysed'})]),
    ]),
])


@app.callback(
    [Output('page-'+p, 'style') for p in PATHS]
    + [Output('nav-'+p,'className') for p in PATHS],
    Input('route','pathname'))
def navigate(path):
    active=current_page(path)
    return [{'display':'block'} if p==active else {'display':'none'} for p in PATHS] + [
        'nav-link active' if p==active else 'nav-link' for p in PATHS]


@app.callback(Output('quality-summary','children'), Output('quality-content','children'),
              Input('refresh','n_intervals'), Input('route','pathname'),
              Input('ai-revision','data'))
def update_quality(_tick, path, _revision):
    # The statistics are only needed when Data quality is open.
    if current_page(path) != 'quality':
        return no_update, no_update
    with closing(db_read()) as db:
        overview = summary_counts(db)
        detail = quality_counts(db)
    return render_summary(overview), render_quality(detail)


def activity_panel(title, entries, build, more_path):
    return html.Section(className='surface activity-panel', children=[
        html.Div(className='activity-heading', children=[html.H2(title),
            dcc.Link('View all →',href=more_path,className='text-link')]),
        html.Div([html.Div(build(r),className='activity-row') for r in entries]
                 if entries else [html.Div('No records',className='empty-list')]),
    ])


@app.callback(Output('overview-operations','children'),Input('ops-refresh','n_intervals'),
              Input('route','pathname'))
def update_operations(_tick, path):
    if current_page(path)!='overview':
        return no_update
    with closing(db_read()) as db:
        records = record_status(db)
    return render_overview(records, latest_runs())


@app.callback(Output('overview-feed','children'),Input('refresh','n_intervals'),
              Input('route','pathname'),Input('ai-revision','data'))
def update_overview(_tick,path,_revision):
    if current_page(path)!='overview': return no_update
    with closing(db_read()) as db:
        news_rows,filing_rows,item_rows=recent_activity(db)
    return [
        activity_panel('Latest news',news_rows,
           lambda r:[plain_link(r['title'],'news','news',r['news_id']),
                     html.Small(f"{stamp(r['published_utc'])}  ·  {r['status'].capitalize()}")],'/news'),
        activity_panel('Latest SEC filings',filing_rows,
           lambda r:[plain_link(f"{r['company_name']} · {r['form_type']}",'filings','form',r['id']),
                     html.Small(stamp(r['filed_date']))],'/filings'),
        activity_panel('Recently extracted Items',item_rows,
           lambda r:[plain_link(f"{r['company_name']} · Item {r['item_type']}",'items','item',r['id']),
                     html.Small(stamp(r['created_at']))],'/items'),
    ]


def show_table(page_name, db_result, headers, render):
    records, count, page = db_result
    return table(headers,[render(r) for r in records]), f'{count:,} records · Page {page:,} of {max(1,(count+23)//24):,}'


@app.callback(Output('companies-table','children'),Output('companies-count','children'),
              Input('companies-search','value'),Input('companies-sort','value'),
              Input('companies-page','value'),Input('refresh','n_intervals'),Input('route','pathname'))
def update_companies(search,sort,page,_tick,path):
    if current_page(path)!='companies':return no_update,no_update
    # Company aggregates are computed by ingestion/AI refresh, never in a page callback.
    with closing(db_read()) as db:
        result=companies(db,search,sort,page)
    return show_table('companies',result,
        ['Company','Ticker','CIK','7d impact','7d monetary (USD)','7d confidence','Filings','Linked news'],lambda r:[
        plain_link(r['name'],'companies','cik',r['cik']),r['ticker'] or '—',r['cik'],
        f"{r['acumm_impact']:+.2f}",f"{r['acumm_monetary']:+,.2f}",
        f"{r['accum_confdent']:.2f} / 10",
        f"{r['filings']:,}",f"{r['news_count']:,}"])



@app.callback(Output('filings-table','children'),Output('filings-count','children'),
              Input('filings-search','value'),Input('filings-kind','value'),
              Input('filings-status','value'),Input('filings-page','value'),
              Input('refresh','n_intervals'),Input('route','pathname'))
def update_filings(search,kind,status,page,_tick,path):
    if current_page(path)!='filings':return no_update,no_update
    with closing(db_read()) as db:result=filings(db,search,kind,status,page)
    return show_table('filings',result,['Company','Form','Filed (UTC)','Items','State'],lambda r:[
        plain_link(r['company_name'],'filings','form',r['id']),
        r['form_type'],stamp(r['filed_date']),r['item_count'],tag(r['status'])])


@app.callback(Output('items-table','children'),Output('items-count','children'),
              Input('items-search','value'),Input('items-status','value'),
              Input('items-page','value'),Input('refresh','n_intervals'),Input('route','pathname'),
              Input('ai-revision','data'))
def update_items(search,status,page,_tick,path,_revision):
    if current_page(path)!='items':return no_update,no_update
    with closing(db_read()) as db:result=items(db,search,status,page)
    return show_table('items',result,['Item','Company','Extracted (UTC)','Status'],lambda r:[
        plain_link('Item '+r['item_type']+' · '+r['item_name'],'items','item',r['id']),
        r['company_name'],stamp(r['created_at']),tag(r['status'])])


@app.callback(Output('news-table','children'),Output('news-count','children'),
              Input('news-search','value'),Input('news-status','value'),
              Input('news-page','value'),Input('refresh','n_intervals'),Input('route','pathname'),
              Input('ai-revision','data'))
def update_news(search,status,page,_tick,path,_revision):
    if current_page(path)!='news':return no_update,no_update
    with closing(db_read()) as db:result=news(db,search,status,page)
    return show_table('news',result,['Headline','Published (UTC)','Links','Status'],lambda r:[
        html.Div([plain_link(r['title'],'news','news',r['news_id']),
                  html.Small(r['source'],className='secondary')]),
        stamp(r['published_utc']),r['links'],tag(r['status'])])


def section(title,contents):
    return html.Section(className='detail-section',children=[html.H3(title),*contents])


def field(label,value):
    return html.Div(className='detail-field',children=[
        html.Span(label),html.Strong(str(value) if value not in (None,'') else '—')])

def numeric(value, spec):
    # Unified relationships persist numeric assessments as REAL, including
    # integer-looking impacts (4.0). Unassessed fields remain NULL, not zero.
    if value is None:
        return '—'
    try:
        return format(float(value), spec)
    except (ValueError, TypeError, OverflowError):
        return '—'


def record_header(title, subtitle, page):
    return html.Div(className='record-head', children=[
        dcc.Link('← Close',href=PATHS[page],className='close-link'),
        html.H2(title),html.P(subtitle or '')])


def company_metric(label, number, unit=''):
    return html.Div(className='company-metric',children=[
        html.Small(label),html.Strong(number),html.Span(unit)])


def company_assessment_card(row):
    label='News' if row['source_type']=='news' else 'SEC Item'
    target=plain_link(row['title'], 'news' if row['source_type']=='news' else 'items',
                      'news' if row['source_type']=='news' else 'item',row['source_id'])
    detail=[
        html.Div(className='company-assessment-head',children=[
            html.Span(label, className='company-kind'),
            html.Small(f"{stamp(row['event_time'])} · {row['age_days']:.2f} days old · weight {row['age_weight']:.3f}")]),
        target,
    ]
    if row['correlation_reason']:
        detail.append(html.P('Connection: '+row['correlation_reason'],className='reason'))
    detail.append(html.Div(className='company-assessment-values', children=[
        field('Impact',numeric(row['impact'],'+g')+' / 10'),
        field('Confidence',numeric(row['confidence'],'.2f')+' / 10'),
        field('Monetary (USD)',numeric(row['monetary'],'+,.2f')),
        field('7-day stock scenario',numeric(row['price_pct'],'+.2f')+'%')
    ]))
    detail.extend([
        html.P('Impact reason: '+(row['reason'] or 'Not stated'),className='reason'),
        html.P('Evidence: '+(row['evidence'] or 'Not stated'),className='reason'),
        html.P('Monetary reason: '+(row['monetary_reason'] or 'Not stated'),className='reason'),
        html.P('Stock scenario reason: '+(row['price_reason'] or 'Not stated'),className='reason'),
        extlink('Original source',row['source_url']),
    ])
    return html.Div(className='company-assessment',children=detail)


def company_panel(db,cik):
    c, forms, articles=company_detail(db,cik)
    if c is None:return html.Div('Company not found.',className='empty-list')
    assessed=company_assessments(db,c['id'])
    item_rows=[r for r in assessed if r['source_type']=='item']
    news_rows=[r for r in assessed if r['source_type']=='news']
    return [record_header(c['name'],(' · '.join(filter(None,[c['ticker'],c['exchange']]))),'companies'),
        html.Div(className='company-summary',children=[
            company_metric('7-day cumulative impact',f"{c['acumm_impact']:+.2f}",'age × confidence weighted points'),
            company_metric('7-day cumulative monetary',f"${c['acumm_monetary']:+,.2f}",'USD · reported AI estimates'),
            company_metric('7-day aggregate confidence',f"{c['accum_confdent']:.2f} / 10",'age weighted mean')]),
        html.P(f"Last 7 days · {len(news_rows)} analysed news links · {len(item_rows)} analysed Items. "
               'Impact = sum(impact × confidence/10 × age weight); monetary = sum(USD × age weight); '
               'confidence = age-weighted mean. Age weight falls linearly from 1 today to 0 at 7 days. '
               'Estimates may overlap; 0 USD can mean unquantifiable, not observed zero.',className='company-formula'),
        section('Company details',[field('CIK',c['cik']),
            extlink('SEC filings',f"https://www.sec.gov/edgar/browse/?CIK={quote(c['cik'])}")]),
        section(f'Analysed news · last 7 days ({len(news_rows)})',
            [company_assessment_card(r) for r in news_rows] or [html.P('No analysed news in the last 7 days')]),
        section(f'Analysed SEC Items · last 7 days ({len(item_rows)})',
            [company_assessment_card(r) for r in item_rows] or [html.P('No analysed Items in the last 7 days')]),
        section('Latest filings',[html.Div(plain_link(f"{x['form_type']} · {stamp(x['filed_date'])}",
            'filings','form',x['id']),className='detail-list-item') for x in forms]
            or [html.P('No filings stored')]),
        section('Latest linked news · all statuses',[html.Div(className='detail-list-item',children=[
            plain_link(x['title'],'news','news',x['news_id']),
            html.P(x['correlation_reason'] or 'No recorded reason',className='reason')])
            for x in articles] or [html.P('No linked news')])]


def filing_panel(db,form):
    f, rows=filing_detail(db,form)
    if f is None:return html.Div('Filing not found.',className='empty-list')
    return [record_header(f"{f['form_type']} · {f['company_name']}",stamp(f['filed_date']),'filings'),
        section('Processing',[field('State',f['status']),field('Extracted Items',len(rows)),
          html.P('Last error: '+f['error'],className='error') if f['error'] else html.Span()]),
        section('SEC documents',[extlink('Filing index',f['index_url']),
            extlink('Primary report',f['primary_url']),
            extlink('Saved report','/reports/'+f['file_guid']) if f['file_guid'] else html.Span()]),
        section('Extracted Items',[
            html.Div(className='detail-list-item', children=[
                plain_link('Item '+r['item_type']+' · '+r['item_name'],'items','item',r['id']),
                html.Small('Extracted: '+stamp(r['created_at'])),tag(r['status'])]) for r in rows]
            or [html.P('No Items extracted yet')])]


def ai_metric(title,value,explanation):
    return html.Div(className='ai-metric', children=[
        html.Small(title),html.Strong(str(value)),
        html.P(explanation or 'Not stated')])


def item_panel(db,item):
    r=item_detail(db,item)
    if r is None:return html.Div('Item not found.',className='empty-list')
    blocks=[record_header('Item '+r['item_type']+' · '+r['item_name'],r['company_name'],'items'),
        html.Div(className='record-status',children=[tag(r['status']),html.Span('Extracted '+stamp(r['created_at']))]),
        section('Filing',[plain_link(r['form_type']+' · '+stamp(r['filed_date']),'filings','form',r['form_id']),
               extlink('SEC source',r['primary_url'] or r['index_url'])])]
    if r['status']=='analysed':
        blocks.append(section('AI assessment',[html.Div(className='ai-metrics',children=[
            ai_metric('Business impact',numeric(r['ai_impact'],'+g')+' / 10',r['ai_reason']),
            ai_metric('Confidence',numeric(r['ai_confidence'],'.1f')+' / 10',r['ai_evidence']),
            ai_metric('Monetary effect (USD)',numeric(r['ai_monetary_effect'],',.2f'),r['ai_monetary_effect_reason']),
            ai_metric('7-day scenario',numeric(r['price_change_1w_pct'],'+.2f')+'%',r['price_change_reason']),
        ])]))
    if r['errors']:
        blocks.append(html.P('Processing error: '+r['errors'],className='error'))
    blocks.append(section('Original Item text',[html.Pre(r['item_text'] or 'No extracted text',className='source-body')]))
    return blocks


def article_panel(db,news_id):
    n,related=news_detail(db,news_id)
    if n is None:return html.Div('Article not found.',className='empty-list')
    full=(n.get('article_text') or '').strip()
    summary=(n.get('summary') or '').strip()
    body=full or summary or n['title']
    blocks=[record_header(n['title'],(n['source'] or '')+' · '+stamp(n['published_utc']),'news'),
        html.Div(className='record-status',children=[tag(n['status'])]),
        extlink('Read original article',n['url']),
        section('Article text' if full else 'RSS summary' if summary else 'Headline',[
            html.Pre(body,className='source-body')]),
        section(f'Linked entities · {len(related)}',[
           html.Div(className='related-company',children=[
               html.Div(className='related-company-head',children=[
                   plain_link(r['entity_name'],'companies','cik',r['cik']) if r['cik']
                   else html.Strong(r['entity_name']), tag(r['status'])]),
               html.P(r['correlation_reason'] or 'No recorded correlation reason',className='reason'),
               html.Div(className='ai-metrics',children=[
                   ai_metric('Business impact',numeric(r['ai_impact'],'+g')+' / 10',r['ai_reason']),
                   ai_metric('Confidence',numeric(r['ai_confidence'],'.1f')+' / 10',r['ai_evidence']),
                   ai_metric('Monetary effect (USD)',numeric(r['ai_monetary_effect'],',.2f'),r['ai_monetary_effect_reason']),
                   ai_metric('7-day scenario',numeric(r['price_change_1w_pct'],'+.2f')+'%',r['price_change_reason'])
               ]) if r['status']=='analysed' else html.Span(),
           ]) for r in related] or [html.P('No company correlations saved')]),
    ]
    return blocks


@app.callback([Output(p+'-detail-content','children') for p in ('companies','filings','items','news')]
              + [Output(p+'-workspace','className') for p in ('companies','filings','items','news')],
              Input('route','pathname'),Input('route','search'),Input('ai-revision','data'))
def load_record(path, search, _rev):
    selected=current_page(path)
    spec={'companies':('cik',company_panel),'filings':('form',filing_panel),
          'items':('item',item_panel),'news':('news',article_panel)}
    outputs=[];classes=[]
    with closing(db_read()) as db:
        for name,(key,panel) in spec.items():
            record=query_id(search,key) if name==selected else ''
            if name in ('filings','items') and record:
                record=int(record) if record.isdecimal() else ''
            if record:
                outputs.append(panel(db,record))
                classes.append('workspace-grid has-detail')
            else:
                outputs.append(html.Div('Select a record to see its details.',className='empty-detail'))
                classes.append('workspace-grid')
    return outputs+classes


@app.callback(Output('item-action-bar','style'),Input('route','pathname'),Input('route','search'))
def show_item_actions(path,search):
    return {'display':'block'} if current_page(path)=='items' and query_id(search,'item').isdecimal() else {'display':'none'}


@app.callback(Output('ai-feedback','children'),Output('ai-revision','data'),
              Input('ai-analyse','n_clicks'),State('route','pathname'),State('route','search'),
              State('ai-revision','data'),prevent_initial_call=True)
def run_selected_analysis(clicks,path,search,revision):
    item_id=query_id(search,'item') if current_page(path)=='items' else ''
    if not clicks or not item_id.isdecimal():return 'Select an Item first.',no_update
    from ai.client import openai_client
    from ai.interactive import analyze_selected
    with closing(db_read()) as db:
        r=item_detail(db,int(item_id))
    if not r:return 'Item not found.',no_update
    try:
        with closing(connect(DB_PATH)) as db:
            with openai_client() as client:
                analyze_selected(db,client,r['id'],r['form_id'])
        return 'AI assessment saved.',(revision or 0)+1
    except Exception as exc:
        message=brief_ai_error(exc)
        try:
            with closing(connect(DB_PATH)) as db:
                with db:
                    db.execute("""UPDATE raw_items SET
                       metadata_json=json_set(metadata_json,'$.errors',?),
                       updated_at=CURRENT_TIMESTAMP
                       WHERE source_type='item' AND
                         (id=? OR CAST(json_extract(metadata_json,'$.id') AS INTEGER)=?)""",
                       (message,r['id'],r['id']))
        except Exception:
            pass
        log_step('ai.item','dashboard_failed',item_id=r['id'],form_id=r['form_id'],reason=message,level='error')
        return 'AI assessment failed: '+message,(revision or 0)+1


@app.callback(Output('ai-answer','children'),Input('ai-ask','n_clicks'),
              State('route','pathname'),State('route','search'),State('ai-question','value'),
              prevent_initial_call=True)
def ask_item(clicks,path,search,question):
    item_id=query_id(search,'item') if current_page(path)=='items' else ''
    if not clicks or not item_id.isdecimal():return 'Select an Item first.'
    with closing(db_read()) as db:r=item_detail(db,int(item_id))
    if not r:return 'Item not found.'
    from ai.client import openai_client
    from ai.interactive import ask_about_item
    try:
        with closing(db_read()) as db:
            with openai_client() as client:
                return ask_about_item(db,client,r['id'],r['form_id'],question)
    except Exception as exc:
        return 'AI question failed: '+brief_ai_error(exc)


register_mobile_callbacks(app, db_read)
register_db_explorer_callbacks(app, db_read)


if __name__=='__main__':
    app.run(host=os.environ.get('SEC_DASH_HOST','127.0.0.1'),
            port=int(os.environ.get('SEC_DASH_PORT','8050')),debug=False)
