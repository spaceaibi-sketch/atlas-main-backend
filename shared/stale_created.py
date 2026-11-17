"""Expire unprocessed News, SEC filing and SEC Item sources by created_at.

Only canonical raw_items rows still marked 'created' are eligible. Already
connected, analysed or skipped records remain intact; no rows are deleted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from shared.processing_log import log_step

_ALLOWED = frozenset({'news', 'filing', 'item'})
STALE_CREATED_HOURS = 24


def skip_created(db, *, sources=('news', 'filing', 'item'), hours=STALE_CREATED_HOURS, force=False,
                 dry_run=False, now=None):
    """Return the number of eligible/skipped records by source type.

    ``created_at`` is the sole age anchor (never event_at or updated_at).
    Records become eligible as soon as 24 full hours have elapsed.
    Forced mode ignores age and even missing/malformed created_at values.
    """
    sources = tuple(dict.fromkeys(sources))
    if not sources or any(source not in _ALLOWED for source in sources):
        raise ValueError('sources must contain news, filing and/or item')
    if hours < 0:
        raise ValueError('hours must not be negative')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must include timezone')
    stamp = now.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    cutoff = (now.astimezone(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec='microseconds').replace('+00:00', 'Z')
    # SQLite julianday() handles UTC Z, offsets and ordinary SQLite UTC stamps.
    # NULL/malformed dates remain pending for inspection, not silently expired.
    # A legacy/migrated source can still say created despite an already-saved
    # relationship assessment. Never expire it and hide valid SEC/news scores.
    where = ("source_type=? AND status='created' AND NOT EXISTS ("
             "SELECT 1 FROM relationships r WHERE r.source_id=raw_items.id "
             "AND r.ai_impact IS NOT NULL)")
    if not force:
        where += ' AND created_at IS NOT NULL AND julianday(created_at)<=julianday(?)'
    reason = ('Forced manual skip of an unconnected created record.' if force
              else f'Created record at least {hours} hours old without completed processing.')
    results = {}
    if dry_run:
        for source in sources:
            args = (source,) if force else (source, cutoff)
            results[source] = db.execute('SELECT COUNT(*) FROM raw_items WHERE ' + where, args).fetchone()[0]
    else:
        with db:
            for source in sources:
                args = (stamp, reason, reason, source) if force else (stamp, reason, reason, source, cutoff)
                cursor = db.execute('''UPDATE raw_items SET status='skipped', updated_at=?,
                    metadata_json=json_set(metadata_json,
                        '$.stage','skipped', '$.skip_reason',?,
                        '$.filtration_reason',?)
                    WHERE ''' + where, args)
                results[source] = cursor.rowcount
        log_step('atlas.stale_created', 'forced_skipped' if force else 'expired_skipped',
                 source_counts=results, hours=None if force else hours, timestamp_utc=stamp)
    return results
