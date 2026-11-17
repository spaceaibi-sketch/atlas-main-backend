"""One append-only structured processing log shared by every Atlas pipeline.

Every event is one JSON object/line with UTC timestamp, stage, action and context.
Data is logged as references, exact decisions, numeric outputs and explanations;
not raw document/article bodies, model prompts, HTTP headers or credentials.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

_LOCK = RLock()
_LOGGER = logging.getLogger('atlas.processing')
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False


def log_path() -> Path:
    override = os.getenv('ATLAS_LOG_PATH')
    if override:
        return Path(override).expanduser().resolve()
    from sec_edgar.settings import database_path
    return database_path().parent / 'atlas_processing.log'


def _ensure_file():
    """Resolve the path lazily, so test db overrides and CLI --db work."""
    path = log_path()
    with _LOCK:
        for handler in list(_LOGGER.handlers):
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path:
                return
            _LOGGER.removeHandler(handler)
            handler.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode='a', encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        _LOGGER.addHandler(handler)


def log_step(stage: str, action: str, *, level: str = 'info', **context):
    """Log a structured pipeline event without changing business behavior.

    Context must be JSON-serializable. Errors in the logging subsystem are
    deliberately non-fatal to extraction and transaction processing.
    """
    try:
        _ensure_file()
        payload = {
            'timestamp_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'stage': stage, 'action': action, 'pid': os.getpid(), **context,
        }
        message = json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)
        getattr(_LOGGER, level.lower(), _LOGGER.info)(message)
    except (OSError, TypeError, ValueError, AttributeError):
        # Failure to write diagnostics cannot risk business records.
        pass
