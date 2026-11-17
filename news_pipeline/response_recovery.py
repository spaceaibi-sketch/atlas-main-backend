"""Conservative repair of AI formatting errors, never inference of missing decisions.

Responses may include Markdown code fences, a preamble, or a trailing comma.
Only structurally unambiguous payloads are accepted; IDs, identities, and
relationships are still validated by the calling code.
"""
from __future__ import annotations

import json
import re

_MAX_CHARS = 2_000_000
_FENCE = re.compile(r'^\s*```(?:json|text|plaintext)?\s*\n([\s\S]*?)\n```\s*$', re.IGNORECASE)
_TRAILING_COMMA = re.compile(r',(?=\s*[}\]])')


def unwrap_text(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('Empty AI response')
    if len(raw) > _MAX_CHARS:
        raise ValueError('AI response exceeds safe parser size')
    raw = raw.lstrip('\ufeff').strip()
    match = _FENCE.fullmatch(raw)
    return (match.group(1) if match else raw).strip()


def recover_json_object(raw: str) -> dict:
    text = unwrap_text(raw)
    decoder = json.JSONDecoder()
    for start in (0, text.find('{')):
        if start < 0:
            continue
        candidate = text[start:]
        for variant in (candidate, _TRAILING_COMMA.sub('', candidate)):
            try:
                value, end = decoder.raw_decode(variant)
            except json.JSONDecodeError:
                continue
            # A recognized prefix/footer is tolerated; a second JSON object is not.
            if isinstance(value, dict) and not variant[end:].lstrip().startswith(('{', '[')):
                return value
    raise ValueError('AI did not return a recoverable JSON object')
