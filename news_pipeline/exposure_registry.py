"""Optional, user-curated contractual exposure evidence for AI extraction.

A company NOT mentioned in the news can only be proposed when an actual company
contract/project/operation and its sector or geography are documented. This is
input evidence, NOT independent online validation by this application.
"""
from __future__ import annotations
import json
from datetime import date
from pathlib import Path
from sec_edgar.settings import ROOT

DEFAULT_EXPOSURES = ROOT.parent / 'data' / 'company_exposures.json'
from shared.config import integer as config_int
MAX_EXPOSURES = config_int('NEWS_MAX_EXPOSURES',80,minimum=0,maximum=1000)  # explicit cost/context budget; fail, never silently truncate
REQUIRED = ('company','country','sector','business_connection','evidence','source_url')


def load_exposures(path=None, *, today=None):
    file = Path(path) if path else DEFAULT_EXPOSURES
    if not file.exists():
        return []
    raw = json.loads(file.read_text(encoding='utf-8'))
    if not isinstance(raw,list) or len(raw)>MAX_EXPOSURES:
        raise ValueError(f'Exposure registry must contain no more than {MAX_EXPOSURES} records')
    today = today or date.today()
    rows=[]
    for i,record in enumerate(raw):
        if (not isinstance(record,dict) or
                not all(isinstance(record.get(k),str) and record[k].strip() for k in REQUIRED)):
            raise ValueError(f'Incomplete exposure evidence record {i}')
        if not record['source_url'].startswith(('https://','http://')):
            raise ValueError(f'Exposure evidence record {i} needs an explicit source URL')
        if record.get('valid_until') and date.fromisoformat(record['valid_until']) < today:
            continue  # expired connections cannot qualify
        rows.append({k:record[k] for k in (*REQUIRED,'valid_until') if k in record})
    return rows
