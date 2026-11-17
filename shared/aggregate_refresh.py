"""Refresh source-owned company score caches after committed AI scoring.

These are separate materialized views. Failure of one must not block updating
another; the per-company AI assessment has already committed and stays saved.
"""
from dashboard.company_analytics import refresh_company_aggregates
from dashboard.mobile_summary import refresh_company_summaries
from shared.ai_errors import brief_ai_error


def _impact_summary_is_trigger_maintained(db):
    """Avoid an all-company refresh when committed AI writes already update it.

    A partial/old migration is not sufficient; fall back to the Python refresh
    unless the triggers for both news and SEC assessment mutations exist.
    """
    expected = ('atlas_mob_items_au', 'atlas_mob_news_rel_au',
                'atlas_mob_items_ai', 'atlas_mob_news_rel_ai')
    placeholders = ','.join('?' for _ in expected)
    found = db.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
        f"AND name IN ({placeholders})", expected
    ).fetchone()[0]
    return found == len(expected)


def refresh_score_caches(db, log):
    errors = 0
    for name, function in (
        ('seven-day company aggregates', refresh_company_aggregates),
        ('Android first-page company summaries', refresh_company_summaries),
    ):
        try:
            if name == 'Android first-page company summaries' and _impact_summary_is_trigger_maintained(db):
                continue
            function(db)
        except Exception as exc:
            errors += 1
            log.error('%s refresh failed after scores were committed: %s', name, brief_ai_error(exc))
    return errors
