"""Dedicated all-company Massive price collector entry point.

Default: every company with a ticker in the existing SQLite companies table,
over the last seven calendar days. --symbol and --limit are optional debugging
filters, forwarded to the shared price collector when explicitly provided.
"""
from __future__ import annotations

import sys

from .cli import run


def massive_arguments(argv: list[str]) -> list[str]:
    """Use Massive regardless of PRICE_SOURCE without changing shared CLI policy."""
    args = list(argv)
    if any(arg == '--source' or arg.startswith('--source=') for arg in args):
        raise ValueError('Massive-only launcher does not accept --source; use extract_prices.sh to switch providers')
    if not any(arg == '--last' or arg.startswith('--last=') for arg in args):
        args = ['--last', '7', *args]
    return ['--source', 'massive', *args]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        command = massive_arguments(args)
    except ValueError as exc:
        print(f'[PRICE MASSIVE] ERROR {exc}', file=sys.stderr)
        return 2
    return run(command)


if __name__ == '__main__':
    raise SystemExit(main())
