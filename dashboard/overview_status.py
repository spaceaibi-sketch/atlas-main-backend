"""Read-only five-table operations overview.

Record ages are measured from *saved* received_at/updated_at timestamps. A
saved row's status is not a durable status-transition event; the UI names it
accordingly. Stage-run counts come only from actual completed processing-log
entries and are never combined into a fictional cross-pipeline batch.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sec_edgar.settings import database_path

STAGES = {
    'news': (
        ('received', 'news.feed', 'run_completed', 'inserted'),
        ('extracted', 'news.ai', 'run_completed', 'main'),
        ('connected', 'news.ai', 'run_completed', 'connected_news'),
        ('analysed', 'news.scoring', 'run_completed', 'analysed_news'),
    ),
    'item': (
        ('extracted', 'sec.form', 'run_completed', 'items_saved'),
        ('connected', None, None, None),
        ('analysed', 'ai.item', 'run_completed', 'analysed'),
    ),
}


def _parsed(value: Any):
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def age_seconds(value: Any, now: datetime | None = None):
    """Integer seconds since a recorded timestamp; unknown/future => None."""
    date = _parsed(value)
    if date is None:
        return None
    now = now or datetime.now(timezone.utc)
    elapsed = (now.astimezone(timezone.utc) - date).total_seconds()
    return int(elapsed) if elapsed >= 0 else None


def record_status(db, now=None):
    """Read stored canonical events, never infer status transitions or batch IDs."""
    now = now or datetime.now(timezone.utc)
    rows = db.execute('''
        SELECT source_type, COUNT(*) total,
          MAX(strftime('%Y-%m-%dT%H:%M:%fZ',received_at)) received_at,
          MAX(CASE WHEN status='analysed' THEN strftime('%Y-%m-%dT%H:%M:%fZ',updated_at) END) analysed_at,
          MAX(CASE WHEN status='skipped' THEN strftime('%Y-%m-%dT%H:%M:%fZ',updated_at) END) skipped_at,
          SUM(CASE WHEN status='created' THEN 1 ELSE 0 END) created_count,
          SUM(CASE WHEN status='connected' THEN 1 ELSE 0 END) connected_count,
          SUM(CASE WHEN status='analysed' THEN 1 ELSE 0 END) analysed_count,
          SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) skipped_count
        FROM raw_items WHERE source_type IN ('news','item') GROUP BY source_type
    ''').fetchall()
    values = {str(row['source_type']): dict(row) for row in rows}
    links = db.execute('''
        SELECT s.source_type,MAX(strftime('%Y-%m-%dT%H:%M:%fZ',r.created_at)) connected_at,COUNT(*) link_count
        FROM relationships r JOIN raw_items s ON s.id=r.source_id
        WHERE s.source_type IN ('news','item') AND r.entity_type='company'
        GROUP BY s.source_type
    ''').fetchall()
    related = {str(row['source_type']): dict(row) for row in links}
    result = {}
    for kind in ('news', 'item'):
        info = values.get(kind, {})
        rel = related.get(kind, {})
        timestamps = {
            'received': info.get('received_at'),
            'connected': rel.get('connected_at'),
            'analysed': info.get('analysed_at'),
            'skipped': info.get('skipped_at'),
        }
        result[kind] = {
            'total': info.get('total', 0),
            'created': info.get('created_count', 0),
            'connected': info.get('connected_count', 0),
            'analysed': info.get('analysed_count', 0),
            'skipped': info.get('skipped_count', 0),
            'links': rel.get('link_count', 0),
            'times': timestamps,
            'ages': {name: age_seconds(value, now) for name, value in timestamps.items()},
        }
    return result


def _last_events(path: Path, requested: set[tuple[str, str]], max_bytes: int):
    """Read the most recent processing log tail, bounded in memory and disk I/O."""
    if not path.is_file():
        return {}
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            if offset:
                handle.readline()  # discard potentially truncated first JSON line
            lines = handle.readlines()
    except OSError:
        # Status cards must remain functional if optional diagnostics are unreadable.
        return {}
    latest = {}
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        key = (event.get('stage'), event.get('action'))
        if key in requested and key not in latest and _parsed(event.get('timestamp_utc')):
            latest[key] = event
            if len(latest) == len(requested):
                break
    return latest


def latest_runs(path: Path | None = None, max_bytes: int | None = None):
    """Last completed run per stage. Missing log/count remains unknown (None)."""
    path = path or Path(os.environ.get('ATLAS_LOG_PATH') or
                        database_path().parent / 'atlas_processing.log')
    if max_bytes is None:
        try:
            max_bytes = int(os.environ.get('ATLAS_DASH_LOG_SCAN_BYTES', '2097152'))
        except ValueError:
            max_bytes = 2097152
    max_bytes = max(1024, min(max_bytes, 16777216))
    wanted = {(stage, action) for parts in STAGES.values()
              for _, stage, action, _ in parts if stage}
    events = _last_events(Path(path), wanted, max_bytes)
    result = {}
    for kind, stages in STAGES.items():
        runs = {}
        for label, stage, action, count_key in stages:
            event = events.get((stage, action)) if stage else None
            value = event.get(count_key) if event else None
            runs[label] = {'at': event.get('timestamp_utc') if event else None,
                           'count': value if type(value) is int and value >= 0 else None}
        result[kind] = runs
    return result


def _time_text(value):
    date = _parsed(value)
    return date.strftime('%d %b %Y %H:%M:%S UTC') if date else 'No recorded run'


def render_overview(records, runs):
    from dash import html

    def age_tile(label, seconds, timestamp):
        return html.Div(className='ops-age-tile', children=[
            html.Span(label),
            html.Strong(f'{seconds:,} s' if seconds is not None else '—'),
            html.Small(_time_text(timestamp) if timestamp else 'No timestamp saved'),
        ])

    def run_row(label, record):
        return html.Tr([
            html.Th(label, scope='row'),
            html.Td(_time_text(record['at']) if record['at'] else '—'),
            html.Td(f"{record['count']:,}" if record['count'] is not None else '—', className='numeric'),
        ])

    panels=[]
    for kind, title in (('news','News'),('item','Items')):
        entry = records[kind]
        stages = runs[kind]
        panels.append(html.Section(className='surface ops-panel', children=[
            html.H2(title),
            html.P(f"{entry['total']:,} saved records · "
                   f"{entry['created']:,} created · {entry['connected']:,} connected · "
                   f"{entry['analysed']:,} analysed · {entry['skipped']:,} skipped",
                   className='ops-summary'),
            html.H3('Seconds since last saved activity'),
            html.Div(className='ops-age-grid', children=[
                age_tile(label, entry['ages'][key], entry['times'][key])
                for key,label in (('received','Received'),('analysed','Analysed'),
                                  ('connected','Connected'),('skipped','Skipped'))
            ]),
            html.H3('Last completed pipeline runs'),
            html.Table(className='data-table ops-run-table', children=[
                html.Thead(html.Tr([html.Th('Operation'),html.Th('Last batch / run (UTC)'),html.Th('Records')])),
                html.Tbody([run_row(label, stages[key]) for key,label in
                    ((('received','Received'),('extracted','Extracted'),('connected','Connected'),('analysed','Analysed'))
                    if kind=='news' else
                    (('extracted','Extracted'),('connected','Connected'),('analysed','Analysed')))]),
            ]),
        ]))
    return [html.Div(className='ops-panels',children=panels),
            html.P('Ages are measured from stored received/updated timestamps; status timestamps may '
                   'reflect later row updates. The last-run rows report separate completed pipeline '
                   'operations, not one shared batch. "—" means that operation or count is not '
                   'recorded in the available log.', className='ops-note')]
