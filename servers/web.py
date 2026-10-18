"""web MCP server: search the web and read pages as plain text (no browser window, no API key).

web_search uses DuckDuckGo's HTML endpoint; read_page fetches a URL and extracts the readable text.
Everything returned is untrusted page content: Iriis treats it as data, never as instructions.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from servers._common import make_server

mcp = make_server("web")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
HONEST_UA = "IriisAssistant/0.2 (personal voice assistant; https://github.com/amschel99/Iriis-voice)"
TIMEOUT = httpx.Timeout(20, connect=10)
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _client(ua: str = UA) -> httpx.Client:
    return httpx.Client(headers={**BROWSER_HEADERS, "User-Agent": ua}, timeout=TIMEOUT, follow_redirects=True)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _real_url(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<real url>."""
    if "uddg=" in href:
        q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href if href.startswith("http") else "https:" + href


def parse_results(page: str, limit: int) -> list[dict[str, str]]:
    results = []
    blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', page)[1:]
    for b in blocks:
        a = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not a:
            continue
        snippet = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>', b, re.S)
        url = _real_url(html.unescape(a.group(1)))
        if "duckduckgo.com/y.js" in url:      # ads
            continue
        results.append({"title": _clean(a.group(2)), "url": url, "snippet": _clean(snippet.group(1)) if snippet else ""})
        if len(results) >= limit:
            break
    return results


@mcp.tool()
def web_search(query: str, max_results: int = 6) -> str:
    """Search the web. Returns titles, links and snippets of the top results (use read_page to read one)."""
    with _client() as c:
        r = c.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "wt-wt"})
    if r.status_code != 200:
        return f"Search failed (HTTP {r.status_code}). Try the browser tools instead."
    results = parse_results(r.text, max(1, min(max_results, 10)))
    if not results:
        return f"No results for '{query}'."
    return "\n\n".join(f"{i}. {x['title']}\n   {x['url']}\n   {x['snippet']}" for i, x in enumerate(results, 1))


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe", "template"}
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "section", "article", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def html_to_text(page: str) -> tuple[str, str]:
    p = _Text()
    p.feed(page)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    lines = [l.strip() for l in text.split("\n")]
    text = "\n".join(l for l in lines if len(l) > 1)
    return p.title.strip(), re.sub(r"\n{3,}", "\n\n", text)


@mcp.tool()
def read_page(url: str, max_chars: int = 6000) -> str:
    """Fetch a web page and return its readable text (title + main text, no menus or scripts)."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    with _client() as c:
        r = c.get(url)
    if r.status_code in (403, 429):
        # Some sites (e.g. Wikipedia) refuse browser look-alikes but welcome an honest, descriptive agent.
        with _client(HONEST_UA) as c:
            r = c.get(url)
    ctype = r.headers.get("content-type", "")
    if r.status_code != 200:
        return f"Couldn't open {url} (HTTP {r.status_code})."
    if "html" not in ctype and "text" not in ctype:
        return f"{url} is not a web page ({ctype or 'unknown type'})."
    title, text = html_to_text(r.text)
    limit = max(500, min(max_chars, 20000))
    more = f"\n[... {len(text) - limit} more characters]" if len(text) > limit else ""
    return f"{title}\n{r.url}\n\n{text[:limit]}{more}"


if __name__ == "__main__":
    mcp.run()
