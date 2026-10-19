"""web MCP server: search the web and read pages as plain text (no browser window, no API key, nothing to pay).

web_search / news_search use the free `ddgs` metasearch library (it rotates DuckDuckGo, Bing, Brave, Mojeek,
Yahoo, Wikipedia... so one engine rate-limiting us doesn't stop the search), then fall back to DuckDuckGo's HTML
and lite pages, then Mojeek. Results are cached for 10 minutes so retries don't hammer the engines.
read_page fetches a URL and extracts the readable text; PDFs are read with pypdf, and pages that need
JavaScript (or refuse plain HTTP clients) are rendered in a headless Chrome with Playwright.
Everything returned is untrusted page content: Iriis treats it as data, never as instructions.
"""
from __future__ import annotations

import html
import io
import logging
import os
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from servers._common import make_server

mcp = make_server("web")
logging.getLogger("ddgs").setLevel(logging.ERROR)    # "backend X has no results" chatter on stderr

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


def parse_lite(page: str, limit: int) -> list[dict[str, str]]:
    """lite.duckduckgo.com: links are <a class='result-link'>, snippets in <td class='result-snippet'>."""
    links = re.findall(r"""<a[^>]+href=["']([^"']+)["'][^>]*class=["']result-link["'][^>]*>(.*?)</a>""", page, re.S)
    snippets = re.findall(r"""class=["']result-snippet["'][^>]*>(.*?)</td>""", page, re.S)
    out = []
    for i, (href, title) in enumerate(links):
        url = _real_url(html.unescape(href))
        if "duckduckgo.com/y.js" in url:
            continue
        out.append({"title": _clean(title), "url": url, "snippet": _clean(snippets[i]) if i < len(snippets) else ""})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------- search engines (all free, no keys)
_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_CACHE_S = 600
RECENCY = {"day": "d", "week": "w", "month": "m", "year": "y"}


def _cached(key, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_S:
        return hit[1]
    res = fn()
    if res:
        _CACHE[key] = (time.time(), res)
    return res


def _ddgs(query: str, n: int, recency: str | None, news: bool = False, backend: str = "auto") -> list[dict]:
    from ddgs import DDGS
    kw = {"max_results": n, "region": "wt-wt", "timelimit": RECENCY.get(recency or ""), "backend": backend}
    d = DDGS(timeout=12)
    rows = d.news(query, **kw) if news else d.text(query, **kw)
    return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "snippet": r.get("body", ""),
             "date": (r.get("date") or "")[:10], "source": r.get("source", "")}
            for r in rows if (r.get("href") or r.get("url"))]


def _ddg_html(query: str, n: int, recency: str | None) -> list[dict]:
    data = {"q": query, "kl": "wt-wt"}
    if recency in RECENCY:
        data["df"] = RECENCY[recency]
    with _client() as c:
        for url in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
            r = c.post(url, data=data)
            if r.status_code == 200:
                res = parse_results(r.text, n) or parse_lite(r.text, n)
                if res:
                    return res
    return []


def _mojeek(query: str, n: int, recency: str | None) -> list[dict]:
    with _client() as c:
        r = c.get("https://www.mojeek.com/search", params={"q": query})
    if r.status_code != 200:
        return []
    out = []
    for m in re.finditer(r'<a[^>]+class="ob"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?<p class="s">(.*?)</p>', r.text, re.S):
        out.append({"title": _clean(m.group(2)), "url": html.unescape(m.group(1)), "snippet": _clean(m.group(3))})
        if len(out) >= n:
            break
    return out


def _wikipedia(query: str, n: int) -> list[dict]:
    """Wikipedia's search API: keyless and never rate-limits a person, so facts still work when the engines don't."""
    r = httpx.get("https://en.wikipedia.org/w/api.php", timeout=TIMEOUT, headers={"User-Agent": HONEST_UA},
                  params={"action": "query", "list": "search", "srsearch": query, "srlimit": n, "format": "json"})
    r.raise_for_status()
    return [{"title": x["title"], "url": "https://en.wikipedia.org/wiki/" + x["title"].replace(" ", "_"),
             "snippet": _clean(x.get("snippet", "")), "source": "Wikipedia"} for x in r.json()["query"]["search"]]


def _tavily(query: str, n: int, recency: str | None, news: bool = False) -> list[dict]:
    """Tavily's free plan (1,000 searches a month, no card): used first when TAVILY_API_KEY is set."""
    body = {"query": query, "max_results": n, "topic": "news" if news else "general"}
    if recency in RECENCY:
        body["time_range"] = recency
    r = httpx.post("https://api.tavily.com/search", json=body, timeout=TIMEOUT,
                   headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x["url"], "snippet": (x.get("content") or "")[:300],
             "date": (x.get("published_date") or "")[:10]} for x in r.json().get("results", [])]


def _brave_api(query: str, n: int, recency: str | None, news: bool = False) -> list[dict]:
    """Brave Search API free plan: used when BRAVE_API_KEY is set."""
    params = {"q": query, "count": n}
    if recency in RECENCY:
        params["freshness"] = "p" + RECENCY[recency]
    r = httpx.get(f"https://api.search.brave.com/res/v1/{'news' if news else 'web'}/search", params=params, timeout=TIMEOUT,
                  headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"})
    r.raise_for_status()
    rows = r.json().get("results", []) if news else r.json().get("web", {}).get("results", [])
    return [{"title": _clean(x.get("title", "")), "url": x["url"], "snippet": _clean(x.get("description", "")),
             "date": x.get("age", "")} for x in rows]


_STOP = {"the", "and", "for", "with", "what", "who", "how", "when", "where", "which", "best", "price", "today", "near",
         "from", "this", "that", "does", "are", "was", "2025", "2026", "2027"}


def relevant(query: str, results: list[dict]) -> list[dict]:
    """Drop results that share no meaningful word with the query (engines under load return junk pages)."""
    terms = {w for w in re.findall(r"\w{3,}", query.lower()) if w not in _STOP}
    if not terms:
        return results
    return [r for r in results if any(t in f"{r['title']} {r['snippet']} {r['url']}".lower() for t in terms)]


def search(query: str, n: int = 6, recency: str | None = None, news: bool = False) -> tuple[list[dict], list[str]]:
    res, problems = _search(query, n, recency, news)
    return relevant(query, res), problems


def _search(query: str, n: int = 6, recency: str | None = None, news: bool = False,
            _retry: bool = False) -> tuple[list[dict], list[str]]:
    """Free API keys first if set (reliable); otherwise race the keyless engines in parallel and take the first that
    answers (each one rate-limits after a few quick queries, but rarely all at once); if all refuse, pause and
    race again, then Wikipedia.
    Returns (results, problems seen on the way)."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    problems: list[str] = []
    keyed = [(name, fn) for name, env, fn in (("tavily", "TAVILY_API_KEY", _tavily), ("brave", "BRAVE_API_KEY", _brave_api))
             if os.environ.get(env)]
    for name, fn in keyed:
        try:
            res = _cached((name, query, n, recency, news), lambda fn=fn: relevant(query, fn(query, n, recency, news)))
            if res:
                return res, problems
            problems.append(f"{name}: no results")
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name}: {e.__class__.__name__}")
    racers = {be: (lambda be=be: _ddgs(query, n, recency, news, be))
              for be in ("duckduckgo", "yahoo", "brave", "startpage", "mojeek", "google")}
    if not news:
        racers["duckduckgo-html"] = lambda: _ddg_html(query, n, recency)
    hit = _CACHE.get(("race", query, n, recency, news))
    if hit and time.time() - hit[0] < _CACHE_S:
        return hit[1], problems
    pool = ThreadPoolExecutor(len(racers))
    futs = {pool.submit(fn): name for name, fn in racers.items()}
    pending, deadline = set(futs), time.time() + 12
    try:
        while pending and time.time() < deadline:
            done, pending = wait(pending, timeout=deadline - time.time(), return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    res = relevant(query, f.result())
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{futs[f]}: {str(e)[:40] or e.__class__.__name__}")
                    continue
                if res:
                    _CACHE[("race", query, n, recency, news)] = (time.time(), res)
                    return res, problems
                problems.append(f"{futs[f]}: no results")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not _retry:
        time.sleep(4)                       # engines rate-limit bursts; a short pause usually clears it
        res, more = _search(query, n, recency, news, _retry=True)
        if res:
            return res, problems + more
    if not news and _retry:
        try:
            res = _wikipedia(query, n)
            if res:
                return res, problems
        except Exception as e:  # noqa: BLE001
            problems.append(f"wikipedia: {e.__class__.__name__}")
    return [], problems


def _fmt(results: list[dict]) -> str:
    rows = []
    for i, x in enumerate(results, 1):
        meta = " ".join(v for v in (x.get("source", ""), x.get("date", "")) if v)
        rows.append(f"{i}. {x['title']}{f' ({meta})' if meta else ''}\n   {x['url']}\n   {x['snippet']}")
    return "\n\n".join(rows)


@mcp.tool()
def web_search(query: str, max_results: int = 6, recency: str = "") -> str:
    """Search the web. Returns titles, links and snippets of the top results; read_page the best one or two for
    details. recency: "", "day", "week", "month" or "year" to keep only recent pages."""
    res, problems = search(query, max(1, min(max_results, 10)), recency or None)
    if not res:
        return (f"No results for '{query}' ({'; '.join(problems)}). Rephrase with fewer, more general words, "
                "or open a likely site directly with read_page.")
    return _fmt(res)


@mcp.tool()
def news_search(query: str, max_results: int = 6, recency: str = "week") -> str:
    """Latest news articles about a topic, with source and date. recency: "day", "week", "month" or "year"."""
    n = max(1, min(max_results, 10))
    res, problems = search(query, n, recency or None, news=True)
    if not res:
        res, problems = search(query + " news", n, recency or None)
    return _fmt(res) if res else f"No news found for '{query}' ({'; '.join(problems)})."


# ---------------------------------------------------------------------------- reading pages
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


JS_HINTS = ("enable javascript", "requires javascript", "javascript is disabled", "please turn on javascript",
            "checking your browser", "just a moment", "verify you are human", "attention required")


def render(url: str, timeout_s: float = 30.0, wait_for: str = "") -> tuple[str, str, str]:
    """Render a page in headless Chrome (Playwright) and return (html, final url, error). Runs in its own thread so
    Playwright's sync API never meets the server's event loop."""
    out: dict = {}

    def go():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                try:
                    b = p.chromium.launch(channel="chrome", headless=True)
                except Exception:  # noqa: BLE001  (no installed Chrome: Playwright's own Chromium)
                    b = p.chromium.launch(headless=True)
                pg = b.new_page(user_agent=UA, locale="en-GB")
                pg.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
                try:
                    if wait_for:
                        pg.wait_for_selector(wait_for, timeout=8000)
                    else:
                        pg.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                out.update(html=pg.content(), url=pg.url)
                b.close()
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{e.__class__.__name__}: {str(e)[:200]}"

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout_s + 15)
    return out.get("html", ""), out.get("url", url), out.get("error", "" if out else "timed out")


def pdf_text(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    return "\n\n".join(f"[page {i}]\n{(pg.extract_text() or '').strip()}" for i, pg in enumerate(r.pages, 1)), len(r.pages)


def focus(text: str, find: str, width: int = 700) -> str:
    """Only the passages around each occurrence of `find` (any of its words if the phrase isn't there)."""
    low = text.lower()
    terms = [find.lower()] if find.lower() in low else [w for w in re.findall(r"\w{3,}", find.lower()) if w in low]
    spans: list[list[int]] = []
    for t in terms:
        for m in re.finditer(re.escape(t), low):
            spans.append([max(0, m.start() - width // 2), min(len(text), m.end() + width // 2)])
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return "\n...\n".join(text[a:b].strip() for a, b in merged[:12])


@mcp.tool()
def read_page(url: str, max_chars: int = 6000, find: str = "", start: int = 0) -> str:
    """Fetch a web page or PDF and return its readable text (title + main text, no menus or scripts). Pages that
    need JavaScript are rendered in a headless browser automatically. find: return only the passages about this
    (e.g. "price", "opening hours"). start: character offset, to continue a long page where the last read stopped."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    r, err = None, ""
    try:
        with _client() as c:
            r = c.get(url)
            if r.status_code in (403, 429):
                # Some sites (e.g. Wikipedia) refuse browser look-alikes but welcome an honest, descriptive agent.
                r2 = c.get(url, headers={"User-Agent": HONEST_UA})
                r = r2 if r2.status_code == 200 else r
    except httpx.HTTPError as e:
        err = f"{e.__class__.__name__}: {e}"
        if "getaddrinfo" in str(e) or "Name or service" in str(e):
            return f"Couldn't find the site {urlparse(url).netloc}: check the address (it may be misspelt)."
    ctype = r.headers.get("content-type", "") if r is not None else ""
    final = str(r.url) if r is not None else url
    ok = r is not None and r.status_code == 200
    if ok and ("pdf" in ctype or r.content[:5] == b"%PDF-"):
        text, n = pdf_text(r.content)
        title = f"PDF, {n} pages"
    else:
        title, text = "", ""
        if ok and "json" in ctype:
            title, text = "JSON", r.text
        elif ok and ("html" in ctype or "text" in ctype or "xml" in ctype):
            title, text = html_to_text(r.text)
        elif ok:
            return f"{url} is not a web page or PDF ({ctype or 'unknown type'})."
        if title != "JSON" and (len(text) < 400 or any(h in text[:3000].lower() for h in JS_HINTS)):
            page, final2, rerr = render(url)
            if page:
                t2, x2 = html_to_text(page)
                if len(x2) > len(text):
                    title, text, final = t2 or title, x2, final2
            elif not text:
                code = f"HTTP {r.status_code}" if r is not None else err
                return f"Couldn't open {url} ({code}; the headless browser also failed: {rerr})."
    if find:
        hit = focus(text, find)
        text = hit or f"(nothing about '{find}' on this page; here is the start)\n" + text
    limit = max(500, min(max_chars, 20000))
    start = max(0, start)
    chunk = text[start:start + limit]
    more = (f"\n[... {len(text) - start - limit} more characters: read_page again with start={start + limit}]"
            if len(text) > start + limit else "")
    return f"{title}\n{final}\n\n{chunk}{more}"


if __name__ == "__main__":
    mcp.run()
