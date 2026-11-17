"""Write-once original response archives: raw_pages/<service>/<subject>/<kind>/.

Never mutate a fetched document; a SHA-256 sidecar records source and byte
identity. Archives are separate from the UUID report copies served by Dash.
"""
from __future__ import annotations
from shared.processing_log import log_step
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def slug(value:str)->str:
    cleaned=re.sub(r'[^a-zA-Z0-9_.-]+','_',str(value))[:85].strip('._')
    return cleaned or 'unknown'


def archive_bytes(db_path, *, service, subject, kind, url, payload,
                  extension=None, metadata=None):
    if not isinstance(payload,(bytes,bytearray)) or not payload:
        raise ValueError('Refusing to archive an empty or non-binary response')
    root=Path(db_path).resolve().parent/'raw_pages'
    directory=root/slug(service)/slug(subject)/slug(kind)
    directory.mkdir(parents=True,exist_ok=True)
    actual=(extension or Path(urlsplit(url).path).suffix or '.bin').lower()
    suffix=actual if actual in ('.xml','.html','.htm','.xhtml','.txt','.json','.rss','.atom') else '.bin'
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    digest=hashlib.sha256(payload).hexdigest()
    basename=f'{stamp}_{digest[:16]}_{uuid4().hex[:8]}'
    target=directory/(basename+suffix)
    manifest=directory/(basename+'.meta.json' if suffix=='.json' else basename+'.json')
    with target.open('xb') as handle:
        handle.write(payload)
    info={
        'source_url':url,'subject':str(subject),'kind':str(kind),
        'sha256':digest,'bytes':len(payload),
        'received_at_utc':datetime.now(timezone.utc).isoformat(),
        'local_file':target.name,'metadata':metadata or {},
    }
    with manifest.open('x',encoding='utf-8') as handle:
        json.dump(info,handle,indent=2,ensure_ascii=False)
    log_step('archive','saved',service=service,subject=subject,kind=kind,source_url=url,bytes=len(payload),sha256=digest,path=str(target))
    return target


def filing_subject(index_url):
    from urllib.parse import urlsplit
    segments=urlsplit(index_url).path.split('/')
    # SEC Archive pattern: /Archives/edgar/data/<CIK>/<accession-no-dashes>/index.htm
    try:
        i=segments.index('data')
        cik=int(segments[i+1]); acc=segments[i+2]
        if not acc.isdecimal() or len(acc)!=18:raise ValueError('Bad accession folder')
        return f'CIK_{cik:010d}__ACCESSION_{acc}'
    except (ValueError,IndexError):
        raise ValueError('SEC filing index URL is missing a safe CIK/accession folder')


def accession_from_url(index_url):
    name=Path(urlsplit(index_url).path).name
    match=re.search(r'(\d{10}-\d{2}-\d{6})-index',name)
    if not match:raise ValueError('Cannot identify SEC accession from index URL')
    return match.group(1)
