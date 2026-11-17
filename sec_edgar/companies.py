"""Enrich companies using the SEC's official company/ticker/exchange JSON."""
import json
from pathlib import Path
from .database import upsert_company
from .raw_archive import archive_bytes
from shared.config import text as config_text, integer as config_int

SEC_COMPANIES_URL='https://www.sec.gov/files/company_tickers_exchange.json'

def load_sec_company_json(conn,data):
    payload=json.loads(data)
    # The SEC currently publishes {"fields": [...], "data": [[...], ...]}.
    if isinstance(payload,dict) and isinstance(payload.get('data'),list):
        fields=payload['fields']
        needed={'cik','name','ticker','exchange'}
        if not needed.issubset(fields):raise ValueError('Unknown SEC company JSON columns')
        mapping={k:fields.index(k) for k in needed}
        iterator=({k:row[mapping[k]] for k in needed} for row in payload['data'])
    elif isinstance(payload,dict):
        iterator=({'cik':row['cik_str'],'name':row['title'],'ticker':row['ticker'],'exchange':None} for row in payload.values())
    else:raise ValueError('Unexpected SEC company JSON')
    count=0
    with conn:
        for company in iterator:
            if not str(company['cik']).isdigit():continue
            upsert_company(conn,str(company['cik']),company['name'],company['ticker'],company['exchange'],'SEC company directory')
            count+=1
    return count

def refresh_companies(conn,client,db_path,*,force=False):
    stamp=Path(db_path).resolve().parent/'sec_company_catalog.json'
    from datetime import datetime,timezone
    stale=not stamp.exists() or (datetime.now(timezone.utc).timestamp()-stamp.stat().st_mtime>config_int('SEC_COMPANY_REFRESH_HOURS',24,minimum=1)*3600)
    if force or stale:
        url=config_text('SEC_COMPANIES_URL',SEC_COMPANIES_URL)
        data=client.get(url)
        archive_bytes(db_path,service='sec_company_directory',subject='ticker_catalog',
                      kind='json',url=url,payload=data,extension='.json')
        count=load_sec_company_json(conn,data)
        part=stamp.with_suffix('.part');part.write_bytes(data);part.replace(stamp)
        return count
    return 0
