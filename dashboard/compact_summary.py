"""Six compact dashboard rows using a single consistent three-hour UTC window."""
from __future__ import annotations

from datetime import datetime, timezone

from news_pipeline.store import _window_seconds
from news_pipeline.time_window import ensure_utc_now


def summary_counts(db, now=None):
    """Return all-time/3h counts and last timestamps from one snapshot.

    News window = publication time; Item window = initial Item extraction (created_at).
    Extracted = all saved Item rows; parsed = nonempty Items marked processed/analysed;
    analysed = those parsed Items with completed AI assessment.
    """
    # Normalize the comparison clock to UTC once; SQLite strftime('%s', value)
    # normalizes ISO 8601 offsets to UTC epoch seconds, independent of the
    # server's local timezone. Naive injected clocks are rejected.
    now_utc = ensure_utc_now(now)
    low, high = _window_seconds(now_utc)
    row = db.execute('''
        WITH
        companies_total AS (
            SELECT COUNT(*) AS companies FROM companies
        ),
        news_stats AS (
            SELECT
              COUNT(*) AS news_all,
              COALESCE(SUM(status='skipped'),0) AS news_skipped_all,
              COALESCE(SUM(status='connected'),0) AS news_connected_all,
              COALESCE(SUM(status='analysed'),0) AS news_analysed_all,
              COALESCE(SUM(CASE WHEN CAST(strftime('%s', published_utc) AS INTEGER)
                                    BETWEEN :low AND :high THEN 1 ELSE 0 END),0) AS news_3h,
              COALESCE(SUM(CASE WHEN CAST(strftime('%s', published_utc) AS INTEGER)
                                    BETWEEN :low AND :high AND status='skipped' THEN 1 ELSE 0 END),0) AS news_skipped_3h,
              COALESCE(SUM(CASE WHEN CAST(strftime('%s', published_utc) AS INTEGER)
                                    BETWEEN :low AND :high AND status='connected' THEN 1 ELSE 0 END),0) AS news_connected_3h,
              COALESCE(SUM(CASE WHEN CAST(strftime('%s', published_utc) AS INTEGER)
                                    BETWEEN :low AND :high AND status='analysed' THEN 1 ELSE 0 END),0) AS news_analysed_3h,
              MAX(CASE WHEN CAST(strftime('%s', published_utc) AS INTEGER) <= :high
                       THEN strftime('%Y-%m-%dT%H:%M:%SZ', published_utc) END) AS last_news_published,
              MAX(CASE WHEN status='analysed'
                         AND CAST(strftime('%s', updated_at) AS INTEGER) <= :high
                       THEN strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) END) AS last_news_analysed
            FROM atlas_articles
        ),
        item_stats AS (
            SELECT
              COUNT(*) AS items_extracted_all,
              COALESCE(SUM(CASE WHEN status IN ('processed','analysed')
                               AND TRIM(item_text)<>'' THEN 1 ELSE 0 END),0) AS items_parsed_all,
              COALESCE(SUM(CASE WHEN status='analysed'
                               AND TRIM(item_text)<>'' THEN 1 ELSE 0 END),0) AS items_analysed_all,
              COALESCE(SUM(CASE WHEN CAST(strftime('%s', created_at) AS INTEGER)
                                     BETWEEN :low AND :high THEN 1 ELSE 0 END),0) AS items_extracted_3h,
              COALESCE(SUM(CASE WHEN CAST(strftime('%s', created_at) AS INTEGER)
                                     BETWEEN :low AND :high
                                     AND status IN ('processed','analysed')
                                     AND TRIM(item_text)<>'' THEN 1 ELSE 0 END),0) AS items_parsed_3h,
              COALESCE(SUM(CASE WHEN CAST(strftime('%s', created_at) AS INTEGER)
                                     BETWEEN :low AND :high
                                     AND status='analysed'
                                     AND TRIM(item_text)<>'' THEN 1 ELSE 0 END),0) AS items_analysed_3h,
              MAX(CASE WHEN status='analysed'
                         AND CAST(strftime('%s', updated_at) AS INTEGER) <= :high
                       THEN strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) END) AS last_item_analysed,
              MAX(CASE WHEN CAST(strftime('%s', created_at) AS INTEGER) <= :high
                       THEN strftime('%Y-%m-%dT%H:%M:%SZ', created_at) END) AS last_item_extracted
            FROM atlas_items
        )
        SELECT * FROM companies_total
        CROSS JOIN news_stats CROSS JOIN item_stats
    ''', {'low': low, 'high': high}).fetchone()
    return dict(row)


def _metric(label, value, *, date=False):
    from dash import html
    formatted = _timestamp(value) if date else f'{value:,}'
    return html.Span(className='summary-stat', children=[
        html.Span(label, className='summary-stat-label'),
        html.Strong(formatted, className='summary-stat-number', title=formatted),
    ])


def _timestamp(value):
    if not value:
        return '—'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime('%d %b %H:%M UTC')
    except ValueError:
        return value


def render_summary(s):
    """Six compact tiles in two rows, with no accordion or status notices."""
    from dash import html

    def group(title, values, klass='', date=False):
        return html.Div(className=('summary-tile ' + klass).strip(), children=[
            html.Span(title, className='summary-tile-title'),
            html.Div(className='summary-tile-values', children=[
                _metric(label, value, date=date) for label, value in values
            ])
        ])

    return html.Div(className='compact-summary', children=[
        group('Companies', [('Total', s['companies'])]),
        group('News · Total', [('Articles', s['news_all']), ('Skipped', s['news_skipped_all']),
                                ('Connected', s['news_connected_all']), ('Analysed', s['news_analysed_all'])]),
        group('News · 3h UTC', [('Articles', s['news_3h']), ('Skipped', s['news_skipped_3h']),
                             ('Connected', s['news_connected_3h']), ('Analysed', s['news_analysed_3h'])]),
        group('Items · Total', [('Extracted', s['items_extracted_all']),
                                 ('Parsed', s['items_parsed_all']), ('Analysed', s['items_analysed_all'])]),
        group('Items · 3h UTC', [('Extracted', s['items_extracted_3h']),
                              ('Parsed', s['items_parsed_3h']), ('Analysed', s['items_analysed_3h'])]),
        group('Latest · UTC', [('News published', s['last_news_published']),
                                ('News analysed', s['last_news_analysed']),
                                ('Item extracted', s['last_item_extracted']),
                                ('Item analysed', s['last_item_analysed'])], 'summary-tile-latest', date=True),
    ])
