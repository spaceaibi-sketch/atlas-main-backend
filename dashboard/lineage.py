"""News lineage statistics and layout; no inferred states or article-by-article loops.

Every count on the flow uses the SAME article publication-time scope.
The news.status field is the only article processing state.
"""
from __future__ import annotations

from news_pipeline.store import _window_seconds


def news_lineage(db, scope='all', now=None):
    """Grouped SQLite aggregation, with optional rolling 3h publication window.

    Stage 2 partitions stored articles by event grouping. Stage 3 partitions
    only main articles by presence of stored links. Stage 4 partitions only
    linked main articles by complete, consistent relationship AI scoring.
    Historical links outside approved mains are explicitly disclosed.
    """
    if scope not in ('all', '3h'):
        raise ValueError('News lineage scope must be all or 3h')
    low, high = _window_seconds(now)
    values = db.execute('''
        WITH scoped AS (
            SELECT news_id, status FROM atlas_articles
            WHERE ? = 'all' OR CAST(strftime('%s', published_utc) AS INTEGER)
                             BETWEEN ? AND ?
        ),
        per_article AS (
            SELECT s.news_id,s.status,
                   COUNT(r.relation_id) AS links,
                   COALESCE(SUM(CASE WHEN r.status='analysed' THEN 1 ELSE 0 END),0) AS scored,
                   COALESCE(SUM(CASE WHEN r.company_id IS NOT NULL AND r.entity_type='company'
                                     THEN 1 ELSE 0 END),0) AS company_links,
                   COALESCE(SUM(CASE WHEN r.relation_id IS NOT NULL AND
                                          (r.company_id IS NULL OR r.entity_type!='company')
                                     THEN 1 ELSE 0 END),0) AS other_links
            FROM scoped s LEFT JOIN atlas_links r ON r.news_id=s.news_id
            GROUP BY s.news_id
        )
        SELECT COUNT(*) AS stored,
          COALESCE(SUM(CASE WHEN status IN ('extracted','connected','analysed') THEN 1 ELSE 0 END),0) AS main,
          COALESCE(SUM(CASE WHEN status='skipped' AND EXISTS(SELECT 1 FROM atlas_articles n WHERE n.news_id=per_article.news_id AND n.filtration_reason LIKE 'Duplicate%') THEN 1 ELSE 0 END),0) AS duplicates,
          COALESCE(SUM(CASE WHEN status='created' OR (status='skipped' AND NOT EXISTS(
             SELECT 1 FROM atlas_articles n WHERE n.news_id=per_article.news_id
               AND n.filtration_reason LIKE 'Duplicate%')) THEN 1 ELSE 0 END),0) AS ungrouped,
          COALESCE(SUM(CASE WHEN status='skipped' AND NOT EXISTS(
             SELECT 1 FROM atlas_articles n WHERE n.news_id=per_article.news_id
               AND n.filtration_reason LIKE 'Duplicate%') THEN 1 ELSE 0 END),0) AS ungrouped_skipped,
          COALESCE(SUM(CASE WHEN status='created' THEN 1 ELSE 0 END),0) AS created,
          COALESCE(SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END),0) AS skipped,
          COALESCE(SUM(CASE WHEN status='connected' THEN 1 ELSE 0 END),0) AS connected,
          COALESCE(SUM(CASE WHEN status='extracted' THEN 1 ELSE 0 END),0) AS extracted,
          COALESCE(SUM(CASE WHEN status='analysed' THEN 1 ELSE 0 END),0) AS analysed,
          COALESCE(SUM(CASE WHEN status IN ('extracted','connected','analysed') AND links>0
                            THEN 1 ELSE 0 END),0) AS main_linked,
          COALESCE(SUM(CASE WHEN status IN ('extracted','connected','analysed') AND links=0
                            THEN 1 ELSE 0 END),0) AS main_unlinked,
          COALESCE(SUM(CASE WHEN status IN ('extracted','connected','analysed') AND links>0
                            AND scored=links AND status='analysed'
                            THEN 1 ELSE 0 END),0) AS main_complete,
          COALESCE(SUM(CASE WHEN status IN ('extracted','connected','analysed') AND links>0 AND
                            (scored!=links OR status!='analysed')
                            THEN 1 ELSE 0 END),0) AS main_incomplete,
          COALESCE(SUM(CASE WHEN status IN ('extracted','connected','analysed') THEN links ELSE 0 END),0) AS main_links,
          COALESCE(SUM(CASE WHEN status IN ('extracted','connected','analysed') THEN scored ELSE 0 END),0) AS main_scored_links,
          COALESCE(SUM(company_links),0) AS company_links,
          COALESCE(SUM(other_links),0) AS other_links,
          COALESCE(SUM(CASE WHEN status NOT IN ('extracted','connected','analysed') THEN links ELSE 0 END),0) AS nonmain_links
        FROM per_article
    ''', (scope, low, high)).fetchone()
    result = dict(values)
    # A company may appear in many articles; COUNT(DISTINCT) is intentionally
    # not a pipeline step and has a different counting unit than relationships.
    result['distinct_companies'] = db.execute('''
        SELECT COUNT(DISTINCT r.company_id)
        FROM atlas_links r JOIN atlas_articles n ON n.news_id=r.news_id
        WHERE r.company_id IS NOT NULL AND r.entity_type='company'
          AND (?='all' OR CAST(strftime('%s',n.published_utc) AS INTEGER)
                           BETWEEN ? AND ?)
    ''', (scope, low, high)).fetchone()[0]
    result['relations'] = result['company_links'] + result['other_links']
    return result


def _step(title, count, detail):
    """Render one readable step with one supporting fact, without nested help panels."""
    from dash import html
    return html.Div(className='lineage-step-card', children=[
        html.Span(title, className='lineage-step-name'),
        html.Strong(f'{count:,}', className='lineage-step-count'),
        html.Span(detail, className='lineage-step-detail'),
    ])


def render_news_lineage(stats, scope):
    """A short, flat news flow. Counts remain in the same publication-time window."""
    from dash import html
    return html.Div(className='lineage-content', children=[
        html.Div(className='lineage-flow', children=[
            _step('News articles', stats['stored'], 'Saved articles'),
            _step('Main stories', stats['main'],
                  f"{stats['duplicates']:,} duplicates · {stats['ungrouped']:,} awaiting review"),
            _step('Linked stories', stats['main_linked'],
                  f"{stats['main_unlinked']:,} main stories without links"),
            _step('AI assessed', stats['main_complete'],
                  f"{stats['main_incomplete']:,} linked stories awaiting completion"),
        ]),
        html.Div(className='lineage-summary', children=[
            html.Span(f"{stats['distinct_companies']:,} linked companies"),
            html.Span(f"{stats['relations']:,} saved connections"),
        ]),
    ])
