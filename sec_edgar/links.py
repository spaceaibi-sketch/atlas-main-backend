"""External, click-through SEC filing links for Dash markdown tables."""
from __future__ import annotations

from urllib.parse import urlsplit


def secure_sec_url(url: str) -> str:
    """Permit only official https://www.sec.gov/Archives/edgar filing pages."""
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != 'www.sec.gov'
            or not parts.path.startswith('/Archives/edgar/data/')
            or parts.query or parts.fragment
            or any(x in url for x in ('[', ']', '(', ')', '\n', '\r'))):
        raise ValueError(f'Not a valid SEC archive URL: {url!r}')
    return url


def dashboard_markdown_link(url: str) -> str:
    """Use in DataTable markdown column with markdown_options.link_target='_blank'."""
    return f'[Open SEC filing ↗]({secure_sec_url(url)})'
