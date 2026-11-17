"""Canonical SEC filing Item AI scoring CLI.

The existing `python -m ai.cli` entry point remains supported for compatibility.
"""
from .cli import main

if __name__ == '__main__':
    raise SystemExit(main())
