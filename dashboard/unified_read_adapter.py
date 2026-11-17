"""Five-table schema identification (no legacy projections or compatibility views)."""
import sqlite3
from shared.canonical_schema import REQUIRED, table_names


def five_table_only(db: sqlite3.Connection) -> bool:
    return table_names(db) == REQUIRED
