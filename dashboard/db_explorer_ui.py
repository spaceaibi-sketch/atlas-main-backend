"""Admin-only read-only database browser mounted in the existing Dash app."""
from __future__ import annotations

from contextlib import closing
import os

from dash import Input, Output, State, dcc, html, no_update

from dashboard.db_explorer_queries import ExplorerError, browse, columns, list_tables, PAGE_SIZE, authorized


def db_explorer_layout():
    return html.Section(id='page-database', className='workspace-page dbx-page', children=[
        html.Div(className='page-intro', children=[html.Div([
            html.H1('Database explorer'),
            html.P('Browse the five canonical Atlas tables, including historical price bars. Inspect source content, AI assessments, and verified company links. Read-only.'),
        ])]),
        html.Section(className='surface dbx-access', children=[
            html.Label('Admin access token', htmlFor='dbx-key', className='input-label'),
            dcc.Input(id='dbx-key', type='password', debounce=True, persistence=False,
                      placeholder='Enter the database explorer token', className='dbx-input',
                      autoComplete='off'),
            html.Span(id='dbx-access-status', className='dbx-access-status'),
        ]),
        html.Section(className='surface dbx-browser', children=[
            html.Div(className='dbx-toolbar', children=[
                html.Label(['Table', dcc.Dropdown(id='dbx-table', options=[], clearable=False,
                                                placeholder='Select table', className='filter-select')]),
                html.Label(['Sort column', dcc.Dropdown(id='dbx-order', options=[], clearable=False,
                                                       className='filter-select')]),
                html.Label(['Direction', dcc.Dropdown(id='dbx-direction', clearable=False,
                    value='desc', options=[{'label':'Newest / descending','value':'desc'},
                                           {'label':'Oldest / ascending','value':'asc'}],
                    className='filter-select')]),
            ]),
            html.Div(className='dbx-toolbar dbx-filters', children=[
                html.Label(['Filter column', dcc.Dropdown(id='dbx-field', options=[], value=None,
                                           clearable=True, placeholder='All rows', className='filter-select')]),
                html.Label(['Condition', dcc.Dropdown(id='dbx-operator', clearable=False, value='contains',
                    options=[{'label':x,'value':v} for v,x in [
                        ('contains','Contains'),('equals','Equals'),('greater','Greater than'),
                        ('less','Less than'),('null','Is NULL'),('not_null','Is not NULL')]],
                    className='filter-select')]),
                html.Label(['Value', dcc.Input(id='dbx-value', type='text', debounce=True,
                                              placeholder='Filter value', className='dbx-input')]),
                html.Button('Refresh',id='dbx-refresh',n_clicks=0,className='secondary-button'),
            ]),
            html.Div(id='dbx-schema', className='dbx-schema'),
            dcc.Loading(children=html.Div(id='dbx-results',className='dbx-results')),
            html.Div(className='dbx-pagination', children=[
                html.Button('← Previous',id='dbx-prev',n_clicks=0,disabled=True,className='secondary-button'),
                html.Label(['Page ',dcc.Input(id='dbx-page',type='number',min=1,max=10000,step=1,
                                              value=1,className='page-number')]),
                html.Button('Next →',id='dbx-next',n_clicks=0,disabled=True,className='secondary-button'),
                html.Span(id='dbx-count',className='result-count'),
            ]),
        ]),
    ])


def register_db_explorer_callbacks(app, db_read):
    @app.callback(Output('dbx-table','options'),Output('dbx-table','value'),
                  Output('dbx-access-status','children'),
                  Input('route','pathname'),Input('dbx-key','value'),State('dbx-table','value'))
    def unlock(path, token, selected):
        if path != '/database':
            return no_update, no_update, no_update
        if not os.environ.get('ATLAS_DB_EXPLORER_TOKEN'):
            return [], None, 'Disabled: set ATLAS_DB_EXPLORER_TOKEN in the dashboard environment.'
        if not authorized(token):
            return [], None, 'Enter your admin access token to view database contents.'
        with closing(db_read()) as db:
            names = list_tables(db)
        return [{'label':name,'value':name} for name in names], (selected if selected in names else (names[0] if names else None)), f'{len(names)} tables available'

    @app.callback(Output('dbx-schema','children'),Output('dbx-field','options'),
                  Output('dbx-order','options'),Output('dbx-field','value'),Output('dbx-order','value'),
                  Input('dbx-table','value'),Input('dbx-key','value'),
                  State('dbx-field','value'),State('dbx-order','value'))
    def show_schema(table, token, field, order):
        if not authorized(token) or not table:
            return '', [], [], None, None
        try:
            with closing(db_read()) as db:
                attrs=columns(db,table)
        except ExplorerError:
            return 'Table not available.',[],[],None,None
        options=[{'label':r['name'],'value':r['name']} for r in attrs]
        names={r['name'] for r in attrs}
        schema=html.Div([
            html.Div([html.Strong('Columns · '+table),html.Span(f'{len(attrs)} columns',className='result-count')],
                     className='dbx-schema-title'),
            html.Div(className='dbx-columns',children=[html.Div(className='dbx-column',children=[
                html.Strong(r['name']),html.Span(r['type'],className='dbx-type'),
                html.Small('Primary key' if r['primary_key'] else 'Required' if r['not_null'] else 'Nullable'),
            ]) for r in attrs]),
        ])
        return schema,options,options,field if field in names else None,order if order in names else next((r['name'] for r in attrs if r['primary_key']==1), attrs[0]['name'] if attrs else None)

    @app.callback(Output('dbx-page','value'), Input('dbx-prev','n_clicks'),Input('dbx-next','n_clicks'),
                  Input('dbx-table','value'),Input('dbx-field','value'),Input('dbx-operator','value'),
                  Input('dbx-value','value'),Input('dbx-order','value'),Input('dbx-direction','value'),
                  State('dbx-page','value'))
    def move_page(_prev, _next, *_args):
        from dash import ctx
        current = _args[-1] or 1
        changed = ctx.triggered_id
        if changed == 'dbx-next': return min(10000,int(current)+1)
        if changed == 'dbx-prev': return max(1,int(current)-1)
        return 1

    @app.callback(Output('dbx-results','children'),Output('dbx-count','children'),
                  Output('dbx-prev','disabled'),Output('dbx-next','disabled'),
                  Input('dbx-table','value'),Input('dbx-field','value'),Input('dbx-operator','value'),
                  Input('dbx-value','value'),Input('dbx-order','value'),Input('dbx-direction','value'),
                  Input('dbx-page','value'),Input('dbx-refresh','n_clicks'),
                  Input('dbx-key','value'),Input('route','pathname'))
    def show_rows(table, field, operator, value, order, direction, page, _refresh, token, path):
        if path != '/database' or not authorized(token) or not table:
            return '', '', True, True
        try:
            with closing(db_read()) as db:
                result=browse(db,table,field=field,operator=operator or 'contains',
                              value=value,order=order,direction=direction or 'desc',page=page)
        except ExplorerError as exc:
            return html.Div(str(exc),className='dbx-message'),'',True,True
        except Exception:
            # Do not expose DB paths or raw SQL in exception messages.
            return html.Div('Unable to query this table.',className='dbx-message'),'',True,True
        if not result.rows:
            return html.Div('No matching records on this page.',className='dbx-message'),f'Page {result.page}',result.page==1,True
        # Each row is a wrapping field grid. Every column is visible without
        # a horizontal scrollbar; very long values can expand individually.
        cards=[]
        for row_index,row in enumerate(result.rows,start=1+(result.page-1)*PAGE_SIZE):
            record_id = row.get('news_id') or row.get('id') or f'#{row_index}'
            fields=[]
            for col in result.columns:
                text = row[col]
                content = (html.Details([
                    html.Summary(text[:210]+'…'),
                    html.Div(text,className='dbx-expanded-value'),
                ],className='dbx-long') if len(text)>240 else
                    html.Span(text,className='dbx-value dbx-empty' if text in ('','NULL','—') else 'dbx-value'))
                fields.append(html.Div([
                    html.Span(col,className='dbx-field-name'),content,
                ],className='dbx-field dbx-links' if col == 'Linked companies' else 'dbx-field'))
            cards.append(html.Article([
                html.Div([html.Span(f'Record {row_index}',className='dbx-card-number'),
                          html.Strong(str(record_id),className='dbx-card-id')],className='dbx-card-header'),
                html.Div(fields,className='dbx-field-grid'),
            ],className='dbx-record'))
        view=html.Div(cards,className='dbx-records')
        return view,f'{len(result.rows)} records · Page {result.page} · {PAGE_SIZE}/page',result.page<=1,not result.has_next
