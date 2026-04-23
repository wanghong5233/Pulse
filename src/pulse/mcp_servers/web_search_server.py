from __future__ import annotations

import json
import re
import urllib.request

from fastmcp import FastMCP

from pulse.core.tools.web_search import search_web

_MCP = FastMCP("web-search")


def _clean_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


@_MCP.tool
def search(query: str, max_results: int = 5) -> str:
    """Search public web pages and return compact JSON results."""
    safe_max = max(1, min(max_results, 12))
    results = search_web(query, max_results=safe_max)
    payload = [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
        }
        for item in results
    ]
    return json.dumps(payload, ensure_ascii=False)


@_MCP.tool
def scrape_page(url: str, max_chars: int = 5000) -> str:
    """Fetch one page and return cleaned text."""
    bounded = max(500, min(max_chars, 20000))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        content = resp.read().decode("utf-8", errors="ignore")
    cleaned = _clean_html(content)
    return cleaned[:bounded]


if __name__ == "__main__":
    _MCP.run()
