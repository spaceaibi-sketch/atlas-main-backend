#!/usr/bin/env python3
"""History backfill + live mutation tests against a copy of the Atlas SQLite fixture.

Run from any directory: python3 tests/test-accumulated-impact-history.py
"""
from __future__ import annotations
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import tempfile
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/migrate-accumulated-impact-history.py'
FIXTURE = ROOT / 'tests/fixtures/parity.sqlite3'
spec = importlib.util.spec_from_file_location('history_migration', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def series(db, cid):
    return db.execute('''SELECT kind,impact,accumulated_impact,accumulated_event_count,accumulated_news_count
                          FROM company_event_impact_history WHERE company_id=?
                          ORDER BY accumulated_event_count''', (cid,)).fetchall()


def assert_total(db, cid):
    expected = db.execute('''SELECT (SELECT SUM(r.ai_impact) FROM news_company_relations r
                                        JOIN news n ON n.news_id=r.news_id
                                        WHERE r.company_id=? AND r.status='analysed'
                                          AND r.ai_impact IS NOT NULL) AS news_total,
                                    (SELECT SUM(i.ai_impact) FROM items i JOIN forms f ON f.id=i.form_id
                                        WHERE f.company_id=? AND i.status='analysed'
                                          AND i.ai_impact IS NOT NULL) AS sec_total''', (cid, cid)).fetchone()
    total = sum(v for v in expected if v is not None)
    current = series(db, cid)
    if current:
        assert current[-1][2] == total, (cid, current, total)
    else:
        assert expected == (None, None), (cid, expected)


def run():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / 'atlas.sqlite3'
        shutil.copyfile(FIXTURE, path)
        dry = subprocess.run(['python3', str(SCRIPT), '--db', str(path), '--dry-run'], capture_output=True, text=True)
        assert dry.returncode == 0, dry.stderr
        with sqlite3.connect(path) as db:
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='company_event_impact_history'").fetchone()
        applied = subprocess.run(['python3', str(SCRIPT), '--db', str(path), '--apply'], capture_output=True, text=True)
        assert applied.returncode == 0, applied.stderr
        assert len(list(path.parent.glob('*.pre-impact-history-*.bak'))) == 1
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys=ON')
            assert series(db, 1) == [('item', 5.0, 5.0, 1, 0), ('news', -3.0, 2.0, 2, 1)]
            assert series(db, 2) == []
            for cid in (1, 2): assert_total(db, cid)
            # An earlier SEC item correction shifts ALL later historical totals.
            db.execute('UPDATE items SET ai_impact=7 WHERE id=1')
            assert series(db, 1) == [('item', 7.0, 7.0, 1, 0), ('news', -3.0, 4.0, 2, 1)]
            # 0 is an explicitly assessed event, included in the event count.
            db.execute("UPDATE news_company_relations SET status='analysed', ai_impact=0 WHERE relation_id='link2'")
            assert series(db, 2) == [('news', 0.0, 0.0, 1, 1)]
            # Change publication date: same saved impacts, different chronological running history.
            db.execute("UPDATE news SET published_utc='2026-10-01T00:00:00Z' WHERE news_id LIKE '%0001'")
            assert series(db, 1) == [('news', -3.0, -3.0, 1, 1), ('item', 7.0, 4.0, 2, 1)]
            # Identical timestamps are ordered deterministically by kind, then record_key.
            db.execute("UPDATE news SET published_utc='2026-10-11T12:00:00Z' WHERE news_id LIKE '%0001'")
            assert series(db, 1) == [('item', 7.0, 7.0, 1, 0), ('news', -3.0, 4.0, 2, 1)]
            # SEC reassignment rebuilds BOTH companies.
            db.execute('UPDATE forms SET company_id=2 WHERE id=1')
            assert series(db, 1) == [('news', -3.0, -3.0, 1, 1)]
            assert series(db, 2)[-1][2] == 7.0
            db.execute('UPDATE forms SET company_id=1 WHERE id=1')
            # Unassess an event; it disappears and subsequent sums revert.
            db.execute("UPDATE news_company_relations SET status='connected' WHERE relation_id='link1'")
            assert series(db, 1) == [('item', 7.0, 7.0, 1, 0)]
            db.execute("UPDATE news_company_relations SET status='analysed', ai_impact=-3 WHERE relation_id='link1'")
            # Deletes remove historical rows immediately.
            db.execute('DELETE FROM items WHERE id=1')
            assert series(db, 1) == [('news', -3.0, -3.0, 1, 1)]
            # Idempotent rerun also restores any accidentally missing cached rows.
            db.execute('DELETE FROM company_event_impact_history')
            assert series(db, 1) == []
            m.migrate(db)
            assert series(db, 1) == [('news', -3.0, -3.0, 1, 1)]
            assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'atlas_hist_%'").fetchone()[0] == 12
            for cid in (1, 2): assert_total(db, cid)
            db.commit()
        # The saved history is actually persisted, not computed in Java/Android.
        with sqlite3.connect(path) as db:
            assert series(db, 1) == [('news', -3.0, -3.0, 1, 1)]
        src = (ROOT/'src/main/java/space/aibi/atlas/Api.java').read_text()
        assert '"accumulated_impact",assessed?n.get("accumulated_impact")' in src
        assert '"accumulated_impact",assessed?r.get("accumulated_impact")' in src
        assert 'hist.accumulated_impact,hist.accumulated_event_count,hist.accumulated_news_count' in src
        assert '"aggregate_at_news",assessed?n.get("accumulated_impact")' in src
    print('PASS: historical backfill; all-time sum; event ordering; zero; date edits; reassignment; unscore; delete; rerun; API fields')


if __name__ == '__main__':
    run()
