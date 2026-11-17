"""Shared collector configuration. Item lists are policies, not trading signals."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT.parent / 'data' / 'edgar.sqlite3'  # atlas/data, sibling of atlas/main_back_end
FORMS = ('8-K', '10-K', '10-Q')
FORM_DESCRIPTIONS = {
    '8-K': 'Current report',
    '10-K': 'Annual report',
    '10-Q': 'Quarterly report',
}
# 9.01 is purposely NOT in the watchlist. 5.07 and other lower-priority items
# are excluded by default. Edit this list to adjust the project policy.
WATCH_8K_ITEMS = frozenset({
    '1.01', '2.02', '2.03', '2.04', '3.01',
    '3.02', '4.02', '5.02', '7.01', '8.01',
})
ITEM_DESCRIPTIONS = {
    '1.01': 'Material definitive agreement',
    '2.02': 'Financial results',
    '2.03': 'Direct financial obligation',
    '2.04': 'Debt acceleration or increase',
    '3.01': 'Listing compliance or delisting',
    '3.02': 'Unregistered sale of equity securities',
    '4.02': 'Non-reliance on previous financial statements',
    '5.02': 'Director / officer changes',
    '7.01': 'Regulation FD disclosure',
    '8.01': 'Other events',
}

def database_path() -> Path:
    return Path(os.getenv('SEC_DB_PATH', str(DEFAULT_DB))).expanduser().resolve()
