"""Forced manual skip of unscored created News, SEC filings and SEC Items."""
from __future__ import annotations

import argparse
import json
import logging
from contextlib import ExitStack, closing
from pathlib import Path

from sec_edgar.database import connect
from sec_edgar.settings import database_path
from sec_edgar.windows_launcher import extraction_lock, load_env
from shared.stale_created import skip_created

LOG = logging.getLogger('skip_news_filings')
# A forced skip must not race a running news extraction or SEC report parser.
# News and SEC scoring jobs are also locked to avoid simultaneous updates.
_LOCKS = ('analyse_items.lock', 'extract_forms_items.lock', 'extract_recent_filings.lock',
          'news_ai_scoring.lock', 'news_pipeline.lock')


def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        'Force-skip unscored raw_items with status=created for News, SEC filings, '
        'and SEC Items regardless of created_at age. Connected/analysed and scored records are preserved.'))
    parser.add_argument('--db', type=Path, help='Atlas five-table SQLite database')
    parser.add_argument('--source', choices=('all', 'both', 'news', 'filing', 'item'),
                        default='all', help=('all: news, filing, item (default); '
                                              'both: legacy news + filing only'))
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview what would be skipped; change nothing')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s')
    try:
        load_env()
        path = (args.db or database_path()).resolve()
        if not path.is_file():
            raise FileNotFoundError(f'Atlas SQLite database does not exist: {path}')
        sources = (('news', 'filing', 'item') if args.source == 'all'
                   else ('news', 'filing') if args.source == 'both'
                   else (args.source,))
        # Acquire all per-pipeline locks non-blocking before writing anything.
        # Always release already-acquired locks if a later one is busy.
        with ExitStack() as stack:
            for name in _LOCKS:
                acquired = stack.enter_context(extraction_lock(path.parent / name))
                if not acquired:
                    LOG.error('Pipeline busy (%s); no records changed. Retry later.', name)
                    return 75
            with closing(connect(path)) as db:
                counts = skip_created(db, sources=sources, force=True,
                                      dry_run=args.dry_run)
        print(json.dumps({'mode': 'preview' if args.dry_run else 'forced',
                          'created_skipped' if not args.dry_run else 'would_skip': counts,
                          'total': sum(counts.values())}, indent=2))
        return 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted')
        return 130
    except Exception as exc:
        LOG.error('Cannot skip pending sources: %s: %s', type(exc).__name__, exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
