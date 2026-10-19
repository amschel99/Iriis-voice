"""Web search parsing, page text extraction, browser tool exposure/tiers, and shell selection."""
import sys

from iriis.agent.permissions import PermissionManager
from iriis.config import Config, MCPServerConfig, load_mcp_servers
from iriis.mcp_client.manager import MCPManager
from servers.web import html_to_text, parse_results

DDG = """
<div class="result results_links results_links_deep web-result">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.cryopak.com%2Fphase-5%2F&amp;rut=x">Phase 5 <b>PCM</b></a>
  <a class="result__snippet" href="x">Maintains <b>5&deg;C</b> for cold chain.</a>
</div>
<div class="result results_links web-result">
  <a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a>
</div>
<div class="result web-result">
  <a class="result__a" href="https://en.wikipedia.org/wiki/PCM">Phase-change material</a>
  <div class="result__snippet">Latent heat storage.</div>
</div>
"""


def test_search_results_are_parsed_and_ads_skipped():
    r = parse_results(DDG, 5)
    assert [x["url"] for x in r] == ["https://www.cryopak.com/phase-5/", "https://en.wikipedia.org/wiki/PCM"]
    assert r[0]["title"] == "Phase 5 PCM" and r[0]["snippet"] == "Maintains 5°C for cold chain."


def test_page_text_skips_scripts_and_menus():
    title, text = html_to_text("<html><head><title>Cold packs</title><script>evil()</script></head><body>"
                               "<nav>Home | About</nav><h1>PCMs</h1><p>They melt at a fixed point.</p>"
                               "<footer>(c) 2026</footer></body></html>")
    assert title == "Cold packs"
    assert "PCMs" in text and "They melt at a fixed point." in text
    assert "evil" not in text and "Home" not in text and "2026" not in text


def test_blocked_and_excluded_browser_tools_are_hidden():
    servers = {s.name: s for s in load_mcp_servers()}
    b = servers["browser"]
    assert b.enabled and "@playwright/mcp@0.0.83" in " ".join(b.args)
    exposed = MCPManager._exposed
    assert exposed(b, "browser_navigate") and exposed(b, "browser_click") and exposed(b, "browser_fill_form")
    assert not exposed(b, "browser_run_code_unsafe")       # blocked tier: never offered
    assert not exposed(b, "browser_network_request")
    assert not exposed(b, "browser_resize")                # excluded: not useful, saves tokens


def test_browser_tiers():
    pm = PermissionManager(Config(), load_mcp_servers())
    assert pm.decide("browser__browser_navigate", {}).tier == "auto"
    assert pm.decide("browser__browser_snapshot", {}).tier == "auto"
    assert pm.decide("browser__browser_fill_form", {}).tier == "confirm"
    assert pm.decide("browser__browser_file_upload", {}).tier == "confirm"
    assert pm.decide("browser__browser_run_code_unsafe", {}).tier == "blocked"
    assert pm.decide("web__web_search", {}).tier == "auto"


def test_desktop_runs_web_and_browser():
    assert {"web", "browser"} <= set(Config().desktop.servers)


def test_shell_can_use_bash_and_cmd():
    from servers.shell import _argv
    if sys.platform == "win32":
        assert _argv("dir")[0] == "powershell.exe"
        assert _argv("dir", "cmd")[:2] == ["cmd.exe", "/d"]
        argv = _argv("ls", "bash")
        assert argv[0].lower().endswith("bash.exe") or argv[0].endswith("bash")
        assert argv[-1] == "ls"
    else:
        assert _argv("ls", "bash")[0] == "/bin/bash"


def test_relevance_filter_and_focus():
    from servers.web import focus, relevant
    junk = [{"title": "How to get help in Windows", "snippet": "Microsoft support", "url": "https://support.microsoft.com"},
            {"title": "Railway pricing", "snippet": "Hobby plan", "url": "https://railway.com/pricing"}]
    assert [r["url"] for r in relevant("Railway hobby plan price", junk)] == ["https://railway.com/pricing"]
    text = "intro " * 200 + "The population was 4,397,073 in 2019. " + "outro " * 200
    out = focus(text, "population")
    assert "4,397,073" in out and len(out) < 1000


def test_search_falls_through_engines(monkeypatch):
    from servers import web
    web._CACHE.clear()
    monkeypatch.setattr(web, "_ddgs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rate limited")))
    monkeypatch.setattr(web, "_ddg_html", lambda *a, **k: [])
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    monkeypatch.setattr(web, "_wikipedia", lambda q, n: [{"title": "Heat pipe", "url": "https://en.wikipedia.org/wiki/Heat_pipe",
                                                         "snippet": "a heat pipe transfers heat"}])
    res, problems = web.search("how does a heat pipe work", 3)
    assert res and res[0]["url"].endswith("Heat_pipe") and any("rate limited" in p for p in problems)
