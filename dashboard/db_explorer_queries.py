"""Strictly read-only, schema-validated SQLite exploration for the web admin UI.

Only schema-derived identifiers can enter generated SQL. Filter values are bound.
No user-supplied SQL, DDL or updates are supported.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math
import os
from hmac import compare_digest
import sqlite3
import time

PAGE_SIZE = 40


def authorized(key: str | None) -> bool:
    secret = os.environ.get('ATLAS_DB_EXPLORER_TOKEN', '')
    return bool(secret and key and compare_digest(str(key), secret))

MAX_PAGE = 10000
QUERY_SECONDS = 6.0
OPERATORS = frozenset({'contains', 'equals', 'greater', 'less', 'null', 'not_null'})


class ExplorerError(ValueError):
    """A table, column or filter is invalid (or a query took too long)."""


def identifier(name: str) -> str:
    """Quote an SQLite identifier; validation against PRAGMA metadata is also required."""
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ExplorerError('Invalid identifier.')
    return '"' + name.replace('"', '""') + '"'


CANONICAL_ORDER = ('companies', 'raw_items', 'relationships', 'summary', 'price_bars')

def list_tables(db: sqlite3.Connection) -> list[str]:
    names = [row[0] for row in db.execute('''
        SELECT name FROM sqlite_master WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name COLLATE NOCASE
    ''')]
    # Show five canonical physical tables first; no TEMP views are exposed.
    return [name for name in CANONICAL_ORDER if name in names] + [name for name in names if name not in CANONICAL_ORDER]



def columns(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in list_tables(db):
        raise ExplorerError('Select an available table.')
    result = db.execute('PRAGMA table_xinfo(' + identifier(table) + ')').fetchall()
    # SQLite-generated hidden columns are not user-visible data columns.
    return [dict(name=r[1], type=r[2] or 'ANY', not_null=bool(r[3]),
                 default=r[4], primary_key=int(r[5])) for r in result if int(r[6]) == 0]


def _page(value: Any) -> int:
    try:
        return max(1, min(MAX_PAGE, int(value)))
    except (ValueError, TypeError, OverflowError):
        return 1


def _escape_like(text: str) -> str:
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _display_cell(value: Any) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, bytes):
        return f'[BLOB · {len(value):,} bytes]'
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return str(value)


@dataclass(frozen=True)
class BrowseResult:
    columns: list[str]
    rows: list[dict[str, str]]
    page: int
    has_next: bool


def browse(db: sqlite3.Connection, table: str, *, order: str | None = None,
           direction: str = 'desc', field: str | None = None,
           operator: str = 'contains', value: str | None = '',
           page: Any = 1) -> BrowseResult:
    cols = columns(db, table)
    available = {c['name']: c for c in cols}
    if not cols:
        return BrowseResult([], [], 1, False)
    if order not in available:
        order = next((c['name'] for c in cols if c['primary_key'] == 1), cols[0]['name'])
    direction = direction.lower() if isinstance(direction, str) else 'desc'
    if direction not in {'asc', 'desc'}:
        raise ExplorerError('Invalid sort direction.')
    if field and field not in available:
        raise ExplorerError('Invalid filter column.')
    if operator not in OPERATORS:
        raise ExplorerError('Invalid filter operation.')
    args: list[Any] = []
    where = ''
    if field:
        col = identifier(field)
        if operator == 'null':
            where = f' WHERE {col} IS NULL'
        elif operator == 'not_null':
            where = f' WHERE {col} IS NOT NULL'
        elif value not in (None, ''):
            value = str(value)[:250]
            if operator == 'contains':
                where = f" WHERE CAST({col} AS TEXT) LIKE ? ESCAPE '\\'"
                args.append('%' + _escape_like(value) + '%')
            else:
                sql_op = {'equals': '=', 'greater': '>', 'less': '<'}[operator]
                if operator in {'greater', 'less'} and any(t in available[field]['type'].upper() for t in ('INT','REAL','NUM','DEC','DOUBLE','FLOAT')):
                    try:
                        argument = float(value)
                        if not math.isfinite(argument): raise ValueError()
                    except ValueError as exc:
                        raise ExplorerError('Numeric filter requires a number.') from exc
                else:
                    argument = value
                where = f' WHERE {col} {sql_op} ?'
                args.append(argument)
    current_page = _page(page)
    sql = (f'SELECT * FROM {identifier(table)}{where} '
           f'ORDER BY {identifier(order)} {direction.upper()} '
           f'LIMIT ? OFFSET ?')
    args.extend((PAGE_SIZE + 1, (current_page - 1) * PAGE_SIZE))
    # A SELECT cannot write; a timeout also prevents pathological scans from
    # stalling the entire dashboard while other users browse.
    deadline = time.monotonic() + QUERY_SECONDS
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 20000)
    try:
        result = db.execute(sql, tuple(args)).fetchall()
    except sqlite3.OperationalError as exc:
        if 'interrupted' in str(exc).lower():
            raise ExplorerError('Query timed out. Filter on an indexed column.') from exc
        raise
    finally:
        db.set_progress_handler(None, 0)
    preview = result[:PAGE_SIZE]
    names = [c['name'] for c in cols]
    displayed = [{name: _display_cell(row[name]) for name in names} for row in preview]
    if table == 'raw_items' and 'id' in names:
        # All three sources use the same verified company lookup. One batched
        # query serves a page; do not consult obsolete source-specific tables.
        ids = [row['id'] for row in preview]
        linked: dict[int, list[str]] = {key: [] for key in ids}
        if ids:
            marks = ','.join('?' for _ in ids)
            relation_sql = ("SELECT r.source_id,c.name,c.id "
               "FROM relationships r JOIN companies c ON c.id=r.company_id "
               "WHERE r.entity_type='company' AND r.source_id IN (" + marks + ") "
               "ORDER BY c.name COLLATE NOCASE,c.id")
            for source_id, company_name, company_id in db.execute(relation_sql, ids):
                label = f'{company_name} (ID {company_id})'
                if label not in linked[source_id]:
                    linked[source_id].append(label)
        for index, row in enumerate(displayed):
            row['Linked companies'] = ' | '.join(linked.get(ids[index], [])) or '—'
        names.append('Linked companies')
    return BrowseResult(names, displayed, current_page, len(result) > PAGE_SIZE)
