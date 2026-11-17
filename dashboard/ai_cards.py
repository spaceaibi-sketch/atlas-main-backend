"""Reusable number-with-reason card for SEC Item and news analysis."""
from __future__ import annotations

def metric_card(title, value, reason_title, reason, tone='', evidence=False):
    from dash import html
    return html.Section(className='assessment-card ' + tone, children=[
        html.Div(className='assessment-metric', children=[
            html.Span(title, className='assessment-label'),
            html.Strong(value, className='assessment-value'),
        ]),
        html.Div(className='assessment-reason', children=[
            html.H4(reason_title),
            html.Blockquote(reason) if evidence else html.P(reason),
        ]),
    ])
