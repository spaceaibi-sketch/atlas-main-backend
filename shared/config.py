"""Atlas runtime configuration. Persist secrets and tunables in main_back_end/.env.

Explicit environment variables and CLI flags override .env values; no shell execution.
Validate numeric settings immediately rather than silently substituting defaults.
"""
from __future__ import annotations
import os
from sec_edgar.windows_launcher import load_env


def text(name: str, default: str = "") -> str:
    load_env()
    return os.environ.get(name, default)


def integer(name: str, default: int, *, minimum: int | None = None,
            maximum: int | None = None) -> int:
    raw = text(name, str(default))
    try:
        result = int(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise ValueError(f"{name} out of range")
    return result


def number(name: str, default: float, *, minimum: float | None = None,
           maximum: float | None = None) -> float:
    import math
    raw = text(name, str(default))
    try:
        result = float(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(result) or minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise ValueError(f"{name} out of range")
    return result


def boolean(name: str, default: bool = False) -> bool:
    raw = text(name, str(default)).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")
