from shared.processing_log import log_step
from shared.config import integer as config_int, number as config_num
"""SEC HTTPS client with one cross-process file-based limiter (4 starts/second).

Uses an OS file lock, not SQLite, so the project has exactly 3 tables.
"""
import logging
import os
import time
from pathlib import Path
from urllib.parse import urljoin,urlsplit
import requests

LOG=logging.getLogger(__name__)
MIN_INTERVAL=0.25

def shared_rate_state_path(db_path):
    return Path(db_path).resolve().parent/'sec_http_rate.lock'

class SharedRequestPacer:
    def __init__(self,path,interval=MIN_INTERVAL):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.interval=max(MIN_INTERVAL,config_num('SEC_HTTP_MIN_INTERVAL_SECONDS',float(interval),minimum=MIN_INTERVAL))
    def wait(self):
        # This lock covers reading, waiting and writing; concurrent processes
        # cannot bypass the rate cap. Requests are initiated after releasing it.
        self.path.touch(exist_ok=True)
        # r+b is intentional: a+b would append the timestamp regardless of seek,
        # so the next process would reread stale data and violate the rate limit.
        with self.path.open('r+b') as f:
            f.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX)
            try:
                f.seek(0)
                previous=float(f.readline().strip() or b'0')
                delay=max(0.0,previous+self.interval-time.time())
                if delay: time.sleep(delay)
                current=time.time()
                f.seek(0);f.write(f'{current:.9f}\n'.encode('ascii'));f.truncate();f.flush()
            finally:
                f.seek(0)
                if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
                else: fcntl.flock(f.fileno(),fcntl.LOCK_UN)

class SecClient:
    def __init__(self,user_agent,*,rate_state=None,max_attempts=None):
        if not user_agent or '@' not in user_agent or 'example.com' in user_agent.lower():
            raise ValueError('Set SEC_USER_AGENT in .env to your real company/application and email')
        self.session=requests.Session()
        self.session.headers.update({'User-Agent':user_agent,'Accept-Encoding':'gzip, deflate'})
        self.pacer=SharedRequestPacer(rate_state) if rate_state else None
        self.max_attempts=config_int('SEC_HTTP_MAX_ATTEMPTS',3,minimum=1,maximum=10) if max_attempts is None else max(1,int(max_attempts));self.request_count=0
    def get(self,url,*,params=None,allow_404=False):
        for attempt in range(self.max_attempts):
            current=url
            request_params=params
            for redirects in range(6):
                if self.pacer:self.pacer.wait()
                self.request_count+=1
                log_step('sec.http','request',url=current,attempt=attempt+1,redirects=redirects,request_number=self.request_count)
                try:
                    # Explicit redirects: requests auto-following would generate
                    # additional HTTP requests that bypass our rate limiter.
                    response=self.session.get(current,params=request_params,
                                              timeout=(config_num('SEC_HTTP_CONNECT_TIMEOUT_SECONDS',8,minimum=1),
                                                       config_num('SEC_HTTP_READ_TIMEOUT_SECONDS',20,minimum=1)),allow_redirects=False)
                except (requests.ConnectionError,requests.Timeout):
                    if attempt+1>=self.max_attempts:raise
                    time.sleep(min(30,2**attempt));break
                log_step('sec.http','response',url=current,status=response.status_code,attempt=attempt+1,bytes=len(response.content))
                if response.status_code in (301,302,303,307,308):
                    location=response.headers.get('Location','')
                    target=urljoin(current,location)
                    parts=urlsplit(target)
                    if parts.scheme!='https' or parts.hostname not in ('www.sec.gov','sec.gov','data.sec.gov'):
                        raise ValueError('Unexpected external SEC redirect')
                    current=target
                    request_params=None
                    continue
                if allow_404 and response.status_code==404:return None
                if response.status_code in (429,500,502,503,504) and attempt+1<self.max_attempts:
                    retry=int(response.headers.get('Retry-After','0')) if response.headers.get('Retry-After','0').isdigit() else 0
                    pause=max(2**attempt,retry)
                    LOG.warning('SEC HTTP %s; retrying after %ds',response.status_code,pause)
                    time.sleep(min(120,pause))
                    break
                response.raise_for_status()
                if not response.content:raise ValueError('Empty SEC response')
                return response.content
            else:
                raise ValueError('Too many SEC redirects')
        raise RuntimeError('SEC request attempts exhausted')
    def close(self):self.session.close()
