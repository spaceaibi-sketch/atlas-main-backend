"""HTTP client for RSS feeds, independent of SEC request policy."""
from __future__ import annotations
from shared.processing_log import log_step
from shared.config import number as config_num, text as config_text
import time
from urllib.parse import urlparse
import requests

class FeedClient:
    def __init__(self,delay=None,timeout=None):
        self.session=requests.Session();self.timeout=(config_num('NEWS_HTTP_TIMEOUT_SECONDS',15,minimum=1) if timeout is None else timeout)
        self.delay=max(config_num('NEWS_HTTP_MIN_INTERVAL_SECONDS',0.25,minimum=0.21) if delay is None else delay,0.21) # fewer than 5 requests/second in this process
        self.last=0.0
        self.session.headers.update({'User-Agent':config_text('NEWS_HTTP_USER_AGENT','SEC-EDGAR-News-Research/1.0 (RSS ingestion)'),
                                     'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml'})
    def get(self,url):
        if urlparse(url).scheme!='https':raise ValueError('Only HTTPS news sources are accepted')
        wait=self.delay-(time.monotonic()-self.last)
        if wait>0:time.sleep(wait)
        self.last=time.monotonic()
        log_step('news.http','request',url=url)
        resp=self.session.get(url,timeout=self.timeout,allow_redirects=True)
        log_step('news.http','response',url=url,status=resp.status_code,bytes=len(resp.content))
        resp.raise_for_status()
        if len(resp.content)>3_000_000:raise ValueError('RSS response exceeds size limit')
        return resp.content
    def close(self):self.session.close()
