"""Best-effort readable article extraction (without browser automation)."""
from __future__ import annotations
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from sec_edgar.raw_archive import archive_bytes

SKIP_DOMAINS=('news.google.com','sec.gov','www.sec.gov','google.com','x.com','twitter.com')


def extract_article(payload:bytes, max_chars=16000):
    if not payload or len(payload)>2_000_000:return ''
    soup=BeautifulSoup(payload,'html.parser')
    for node in soup(['script','style','nav','footer','header','form','aside','noscript','iframe']):
        node.decompose()
    article=soup.find('article') or soup.find('main')
    if article is None:return ''
    paragraphs=[p.get_text(' ',strip=True) for p in article.find_all(['p','h2','h3'])]
    text='\n'.join(p for p in paragraphs if len(p)>35)
    return re.sub(r'\n{3,}','\n\n',text).strip()[:max_chars]


def should_fetch(url):
    p=urlsplit(url or '')
    return p.scheme=='https' and bool(p.hostname) and not any(
        p.hostname.lower()==d or p.hostname.lower().endswith('.'+d)
        for d in SKIP_DOMAINS)


def article_for_news(client,news,db_path=None):
    if not should_fetch(news['url']):return ''
    payload=client.get(news['url'])
    if db_path is not None:
        archive_bytes(db_path,service='news_articles',subject=news['news_id'],
                      kind='html',url=news['url'],payload=payload,extension='.html')
    return extract_article(payload)
