"""One-time, resumable News schema migration; never calls an AI provider."""
from __future__ import annotations

from contextlib import closing
from sec_edgar.database import connect
from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import extraction_lock, load_env
from .store import ensure_schema


def main():
    load_env()
    path = database_path()
    with extraction_lock(path.parent / 'news_pipeline.lock') as locked:
        if not locked:
            raise RuntimeError('News extractor is active; stop it before migrating')
        with closing(connect(path)) as db:
            ensure_schema(db)
            columns = {row[1] for row in db.execute('PRAGMA table_info(news)')}
            if 'companies' not in columns:
                raise RuntimeError('Migration did not create news.companies')
            report = dict(db.execute('''SELECT COUNT(*) AS articles,
                   SUM(CASE WHEN companies<>'' THEN 1 ELSE 0 END) AS with_companies
                   FROM news''').fetchone())
            report['skipped_duplicates'] = db.execute("SELECT COUNT(*) FROM news WHERE status='skipped' AND filtration_reason LIKE 'Duplicate coverage%' ").fetchone()[0]
            print(f"News migration complete: articles={report['articles']} "
                  f"with_companies={report['with_companies']} "
                  f"skipped_duplicates={report['skipped_duplicates']}")
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
