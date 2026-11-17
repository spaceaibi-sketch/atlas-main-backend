"""Read-only pipeline status and freshness report for the Atlas web dashboard.

Every count is computed from saved SQLite records.  The three-hour column
uses each record's source/ingestion time, not the time it entered its current
status; no historical transition log is available in this schema.
"""
from __future__ import annotations

from datetime import datetime, timezone

from news_pipeline.store import _window_seconds
from dashboard.unified_read_adapter import five_table_only


# Table/column names are code constants only, never supplied by the client.
_UNIFIED_STATUSES = (('created','created'),('skipped','skipped'),
                     ('connected','connected'),('analysed','analysed'))
# These only describe temporary read projections. The sole persisted workflow
# status is raw_items.status; relationships do not have a status column.

_GROUPS = (
    ('filings', 'SEC filings', 'atlas_filings', 'status', 'created_at', 'updated_at',
     (('created', 'created'), ('extracted', 'extracted'),
      ('parsed', 'parsed'), ('error', 'error'))),
    ('items', 'Items', 'atlas_items', 'status', 'created_at', 'updated_at',
     (('created', 'created'), ('processed', 'processed'),
      ('analysed', 'analysed'), ('rejected', 'rejected'), ('error', 'error'))),
    ('news', 'News articles', 'atlas_articles', 'status', 'published_utc', 'updated_at',
     (('created', 'created'), ('skipped', 'skipped'), ('extracted', 'extracted'),
      ('connected', 'connected'), ('analysed', 'analysed'))),
    ('links', 'News/company relationships', 'atlas_links', 'status', 'created_at', 'updated_at',
     (('connected', 'connected'), ('analysed', 'analysed'))),
)


def quality_counts(db, now=None):
    """Compute all-status inventories, freshness and actionable quality checks.

    For news and grouping, 3h means published in the last three hours.
    For filings, Items and relationships it means first saved in that window.
    Statuses are snapshots, not counts of transitions within the window.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must include a timezone')
    low, high = _window_seconds(now)
    params = {'low': low, 'high': high}
    groups = {}
    unified = five_table_only(db)
    for key, title, tablename, statuscol, timecol, changedcol, expected in _GROUPS:
        if unified:
            expected = _UNIFIED_STATUSES
            if key == 'links':
                title = 'Company links (assessment derived)'
        data = db.execute(f'''
            SELECT {statuscol} AS state, COUNT(*) AS total,
              SUM(CASE WHEN CAST(strftime('%s', {timecol}) AS INTEGER)
                                BETWEEN :low AND :high THEN 1 ELSE 0 END) AS recent,
              MAX(NULLIF({changedcol}, '')) AS changed_at
            FROM {tablename}
            GROUP BY {statuscol}
        ''', params).fetchall()
        found = {r['state']: dict(r) for r in data}
        rows = [dict(code=code, name=label, total=found.get(code, {}).get('total', 0),
                     recent=found.get(code, {}).get('recent', 0),
                     changed_at=found.get(code, {}).get('changed_at'))
                for code, label in expected]
        for code in sorted(found.keys() - {x[0] for x in expected}):
            r = found[code]
            rows.append(dict(code=code, name=str(code),
                             total=r['total'], recent=r['recent'], changed_at=r['changed_at']))
        groups[key] = {'title': title, 'rows': rows,
                       'total': sum(r['total'] for r in rows),
                       'recent': sum(r['recent'] for r in rows)}

    activity = dict(db.execute('''
        SELECT
         (SELECT MAX(updated_at) FROM companies) AS last_company_updated,
         (SELECT MAX(created_at) FROM atlas_filings) AS last_filing_discovered,
         (SELECT MAX(updated_at) FROM atlas_filings WHERE status='parsed') AS last_filing_parsed,
         (SELECT MAX(created_at) FROM atlas_items) AS last_item_extracted,
         (SELECT MAX(updated_at) FROM atlas_items WHERE status='analysed') AS last_item_analysed,
         (SELECT MAX(strftime('%Y-%m-%dT%H:%M:%SZ', published_utc)) FROM atlas_articles
           WHERE CAST(strftime('%s', published_utc) AS INTEGER) <= :high) AS last_news_published,
         (SELECT MAX(updated_at) FROM atlas_articles WHERE status='analysed') AS last_news_analysed,
         (SELECT MAX(created_at) FROM atlas_articles) AS last_news_saved,
         (SELECT MAX(created_at) FROM atlas_links) AS last_company_linked,
         (SELECT MAX(updated_at) FROM atlas_links WHERE status='analysed') AS last_link_analysed
    ''', {'high': high}).fetchone())

    checks = dict(db.execute('''
        SELECT
          (SELECT COUNT(*) FROM atlas_filings f WHERE f.status='parsed' AND NOT EXISTS
            (SELECT 1 FROM atlas_items i WHERE i.form_id=f.id)) AS parsed_without_items,
          (SELECT COUNT(*) FROM atlas_filings WHERE status='error') AS forms_failed,
          (SELECT COUNT(*) FROM atlas_items WHERE TRIM(item_text)='') AS empty_items,
          (SELECT COUNT(*) FROM atlas_articles WHERE TRIM(article_text)='') AS news_without_full_text,
          (SELECT COUNT(*) FROM atlas_items WHERE status IN ('error','rejected')) AS items_failed,
          (SELECT COUNT(*) FROM atlas_articles n WHERE n.status IN ('connected','analysed') AND NOT EXISTS
            (SELECT 1 FROM atlas_links r WHERE r.news_id=n.news_id)) AS main_without_links,
          (SELECT COUNT(*) FROM atlas_links WHERE
            TRIM(correlation_reason)='') AS links_missing_reason,
          (SELECT COUNT(*) FROM atlas_articles n WHERE n.status='skipped' AND
              (n.companies!='' OR EXISTS(
                SELECT 1 FROM atlas_links r WHERE r.news_id=n.news_id
              ))) AS invalid_duplicates,
          (SELECT COUNT(*) FROM atlas_articles WHERE CAST(strftime('%s',published_utc) AS INTEGER) > :high)
            AS future_news,
          (SELECT COUNT(*) FROM atlas_articles n WHERE n.status='analysed' AND (
               NOT EXISTS
               (SELECT 1 FROM atlas_links r WHERE r.news_id=n.news_id) OR EXISTS
               (SELECT 1 FROM atlas_links r WHERE r.news_id=n.news_id AND r.status!='analysed')))
            AS inconsistent_news_analysis
    ''', {'high': high}).fetchone())
    return {'groups': groups, 'activity': activity, 'checks': checks,
            'unified': unified, 'as_of': now.isoformat()}


def _date(value):
    if not value:
        return '—'
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(
            timezone.utc).strftime('%d %b %Y · %H:%M UTC')
    except (ValueError, TypeError):
        return str(value)


def render_quality(report):
    """Flat, accessible page: no expandable help, charts, or dependency on browser JS."""
    from dash import html

    activities = (
        ('Last company record updated', 'last_company_updated'),
        ('Last SEC filing discovered', 'last_filing_discovered'),
        ('Last filing parsed', 'last_filing_parsed'),
        ('Last Item extracted', 'last_item_extracted'),
        ('Last Item analysed', 'last_item_analysed'),
        ('Last news saved', 'last_news_saved'),
        ('Last news published', 'last_news_published'),
        ('Last news analysed', 'last_news_analysed'),
        ('Last company linked', 'last_company_linked'),
        ('Last company link analysed', 'last_link_analysed'),
    )
    checks = (
        ('Parsed filings with no Items', 'parsed_without_items'),
        ('Failed filings', 'forms_failed'),
        ('Items without text', 'empty_items'),
        ('News without full article text', 'news_without_full_text'),
        ('Failed or rejected Items', 'items_failed'),
        ('Main stories without links', 'main_without_links'),
        ('Links without a reason', 'links_missing_reason'),
        ('Skipped articles retaining company candidates/links', 'invalid_duplicates'),
        ('Future-dated news', 'future_news'),
        ('News marked analysed without complete assessments', 'inconsistent_news_analysis'),
    )

    def status_panel(key):
        group = report['groups'][key]
        return html.Section(className='surface quality-card', children=[
            html.Div(className='quality-card-header', children=[
                html.H2(group['title']),
                html.Span(f"{group['total']:,} total", className='quality-total'),
            ]),
            html.Table(className='quality-table', children=[
                html.Thead(html.Tr([html.Th('Status',scope='col'),
                                     html.Th('Total',scope='col'),
                                     html.Th('Last 3h',scope='col'),
                                     html.Th('Last updated (UTC)',scope='col')])),
                html.Tbody([html.Tr([
                    html.Th(r['name'],scope='row'),
                    html.Td(f"{r['total']:,}",className='numeric'),
                    html.Td(f"{r['recent']:,}",className='numeric'),
                    html.Td(_date(r['changed_at']), className='quality-datetime'),
                ]) for r in group['rows']]),
                html.Tfoot(html.Tr([html.Th('All statuses',scope='row'),
                                     html.Td(f"{group['total']:,}",className='numeric'),
                                     html.Td(f"{group['recent']:,}",className='numeric'),
                                     html.Td('')])),
            ]),
        ])

    return [
        html.Div(className='quality-activity', children=[
            html.Section(className='surface quality-card', children=[
                html.Div(className='quality-card-header',children=[html.H2('Latest activity')]),
                html.Div(className='quality-time-grid',children=[
                    html.Div(className='quality-time',children=[
                        html.Span(label), html.Strong(_date(report['activity'].get(key)))
                    ]) for label,key in activities]),
            ]),
        ]),
        html.Div(className='quality-section-title',children=[
            html.H2('Status by stage'),
            html.Span('Last 3h: publication time for news; first saved time for filings, Items and links.'),
        ]),
        html.Div(className='quality-panels',children=[status_panel(k) for k in
                  ('filings','items','news','links')]),
        html.Div(className='quality-section-title',children=[html.H2('Data checks')]),
        html.Section(className='surface quality-card',children=[
            html.Div(className='quality-check-grid',children=[
                html.Div(className='quality-check',children=[
                    html.Strong(f"{report['checks'][key]:,}",
                                className='quality-check-count' + (' quality-check-alert' if report['checks'][key] else '')),
                    html.Span(label),
                ]) for label,key in checks]),
        ]),
    ]
