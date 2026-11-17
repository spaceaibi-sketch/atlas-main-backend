"""Five-table-only derived SQL projections for dashboard SELECTs.

Derived CTEs are expanded into individual queries: no persistent tables, views,
TEMP views, triggers, or legacy physical-table access. DB schema is unchanged.
The per-query CTE is intentional: it makes read dependencies explicit and keeps
read-only connections read-only. All source keys remain canonical strings.
"""
from __future__ import annotations
import re

# Keys written by the canonical ingestion layer are persisted in metadata_json;
# inherited historical integer identifiers are retained there too.
PROJECTIONS = """
atlas_filings AS (
 SELECT CAST(COALESCE(json_extract(s.metadata_json,'$.id'),s.id) AS INTEGER) id,
  COALESCE(r.company_id,CAST(json_extract(s.metadata_json,'$.company_id') AS INTEGER)) company_id,
  COALESCE(json_extract(s.metadata_json,'$.form_type'),'') form_type,
  COALESCE(json_extract(s.metadata_json,'$.form_name'),s.title) form_name,
  s.event_at filed_date,s.source_url index_url,
  json_extract(s.metadata_json,'$.primary_url') primary_url,
  json_extract(s.metadata_json,'$.file_guid') file_guid,
  json_extract(s.metadata_json,'$.file_name') file_name,
  COALESCE(json_extract(s.metadata_json,'$.stage'),s.status) status,
  COALESCE(json_extract(s.metadata_json,'$.error'),'') error,
  s.created_at,s.updated_at
 FROM raw_items s LEFT JOIN relationships r ON r.id=(
   SELECT t.id FROM relationships t WHERE t.source_id=s.id AND t.company_id IS NOT NULL
   ORDER BY t.id LIMIT 1)
 WHERE s.source_type='filing'
),
atlas_items AS (
 SELECT CAST(COALESCE(json_extract(s.metadata_json,'$.id'),s.id) AS INTEGER) id,
  CAST(json_extract(s.metadata_json,'$.form_id') AS INTEGER) form_id,
  json_extract(s.metadata_json,'$.file_guid') file_guid,
  json_extract(s.metadata_json,'$.file_name') file_name,
  COALESCE(json_extract(s.metadata_json,'$.item_type'),'') item_type,
  COALESCE(CAST(json_extract(s.metadata_json,'$.item_occurrence') AS INTEGER),1) item_occurrence,
  s.title item_name,s.content item_text,
  CASE WHEN s.status='analysed' THEN s.status ELSE
    COALESCE(json_extract(s.metadata_json,'$.stage'),'processed') END status,
  r.ai_impact,r.ai_confidence,r.ai_reason,r.ai_evidence,
  r.ai_monetary_effect,r.ai_monetary_effect_reason,r.price_change_1w_pct,r.price_change_reason,
  COALESCE(json_extract(s.metadata_json,'$.errors'),'') errors,
  s.created_at,s.updated_at
 FROM raw_items s LEFT JOIN relationships r ON r.id=(
   SELECT t.id FROM relationships t WHERE t.source_id=s.id AND t.entity_type='company'
   ORDER BY t.id LIMIT 1)
 WHERE s.source_type='item'
),
atlas_articles AS (
 SELECT s.source_key news_id,s.title,s.source_url url,
  COALESCE(json_extract(s.metadata_json,'$.url_key'),s.source_key) url_key,
  json_extract(s.metadata_json,'$.title_key') title_key,
  json_extract(s.metadata_json,'$.story_key') story_key,
  COALESCE(s.summary,'') summary,s.content article_text,s.source_name source,
  COALESCE(json_extract(s.metadata_json,'$.region'),'') region,
  COALESCE(json_extract(s.metadata_json,'$.category'),'') category,
  s.event_at published_utc,
  COALESCE(json_extract(s.metadata_json,'$.companies'),'') companies,
  COALESCE(json_extract(s.metadata_json,'$.filtration_reason'),'') filtration_reason,
  CASE WHEN s.status='created' THEN
    COALESCE(json_extract(s.metadata_json,'$.stage'),s.status) ELSE s.status END status,
  COALESCE(json_extract(s.metadata_json,'$.errors'),'') errors,
  s.created_at,s.updated_at
 FROM raw_items s WHERE s.source_type='news'
),
atlas_links AS (
 SELECT COALESCE(json_extract(r.metadata_json,'$.relation_id'),CAST(r.id AS TEXT)) relation_id,
  s.source_key news_id,r.company_id,r.entity_key,r.entity_type,r.entity_name,
  CASE WHEN r.ai_impact IS NOT NULL THEN 'analysed'
       WHEN s.status='skipped' THEN 'skipped' ELSE 'connected' END status,
  r.ai_impact,r.ai_confidence,r.ai_reason,r.ai_evidence,
  r.ai_monetary_effect,r.ai_monetary_effect_reason,
  r.price_change_1w_pct,r.price_change_reason,
  COALESCE(json_extract(r.metadata_json,'$.errors'),'') errors,
  r.created_at,r.updated_at,r.match_hits,r.match_evidence,r.correlation_reason
 FROM relationships r JOIN raw_items s ON s.id=r.source_id AND s.source_type='news'
)
"""
_NAMES = re.compile(r'\batlas_(?:filings|items|articles|links)\b', re.I)
_WITH = re.compile(r'^(\s*WITH\s+)(RECURSIVE\s+)?', re.I)


def with_canonical(sql: str) -> str:
    """Prefix read-only derived CTEs to a SELECT/CTE/INSERT statement."""
    if not _NAMES.search(sql):
        return sql
    match = _WITH.match(sql)
    if match:
        return sql[:match.start()] + f'WITH {match.group(2) or ""}{PROJECTIONS},\n' + sql[match.end():]
    return f'WITH {PROJECTIONS}\n{sql}'


class CanonicalConnection:
    """Minimal sqlite3.Connection forwarding, preserving transaction semantics."""
    def __init__(self, connection):
        self._db = connection

    def execute(self, sql, parameters=()):
        return self._db.execute(with_canonical(sql), parameters)

    def executemany(self, sql, parameters):
        return self._db.executemany(with_canonical(sql), parameters)

    def __enter__(self):
        self._db.__enter__()
        return self

    def __exit__(self, *args):
        return self._db.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._db, name)
