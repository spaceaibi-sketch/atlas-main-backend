"""Optional CEO registry for Phase 1 AI context; no Python evidence gate.

Company Name|Ticker is the SEC catalog. CEO data is a separate, opt-in JSON
source, which this loader schema-validates but does NOT independently verify.
The default file is read only if it exists; CEO input is never derived from the catalog.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, date
from pathlib import Path
from sec_edgar.settings import ROOT

CEOS_FILE = ROOT.parent / 'data' / 'company_ceos.json'


def load_ceos(path=None, *, today=None):
    path=Path(path) if path else CEOS_FILE
    if not path.exists():return []
    records=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records,list) or len(records)>80:
        raise ValueError('CEO evidence must be a list with no more than 80 records')
    today=today or datetime.now(timezone.utc).date()
    out=[]
    for i,x in enumerate(records):
        required=('company','ceo','evidence','source_url')
        if not isinstance(x,dict) or any(not isinstance(x.get(k),str) or not x[k].strip()
                                          for k in required):
            raise ValueError(f'Invalid verified CEO record {i}')
        if not x['source_url'].startswith(('http://','https://')):
            raise ValueError(f'CEO record {i} needs a source URL')
        if len(x['ceo'].split())<2:
            raise ValueError(f'CEO record {i} requires full first and last name')
        if x.get('valid_until') and date.fromisoformat(x['valid_until'])<today:continue
        out.append({k:x[k] for k in (*required,'valid_until') if k in x})
    return out


def validate_resolved(rows, resolved, *, exposures=None, ceos=None):
    """Backward-compatible pass-through; evidence never blocks Phase 1 linking.

    The company identity must have been uniquely resolved by SecCompanyMatcher.
    The Phase 2 AI assessment handles substantive relevance and evidence.
    The old rows/exposures/ceos parameters are retained for legacy callers.
    """
    return resolved
