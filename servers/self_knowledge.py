"""self MCP server: Iriis knows how she works, from her own running code (cloud).

The code she reads is the code that's deployed (every push redeploys), so answers about "how do you work" and
"what can you do" follow the real, current setup instead of guesses. Live status (what's connected right now)
comes from her own /v1/status. Recent changes come from the public GitHub repo.

For the Seryn Twin's code she uses its own seryn_source_* tools (that repo is private; it never leaves Seryn).
Secrets are never readable: no .env files exist in the container, and anything that looks like one is refused.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import yaml

from servers._common import ROOT, make_server

mcp = make_server("self")
REPO = os.environ.get("IRIIS_GITHUB_REPO", "amschel99/Iriis-voice")
TEXT = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".html", ".js", ".ps1", ".cmd", ".sh", ".json"}
SKIP_DIRS = {".git", ".venv", ".venv-convert", ".venv-deploy", "models", "data", "logs", "__pycache__", "node_modules",
             ".pytest_cache", "benchmarks"}
SECRETISH = re.compile(r"(^|[\\/])(\.env(\..*)?|.*secret.*|.*\.pem|.*\.key|credentials.*|token.*\.json)$", re.I)

WHERE = {"cloud": "in the cloud (works even with the computer off)", "desktop": "on the user's computer (needs it on)"}


def _files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix.lower() in TEXT and not (set(p.relative_to(ROOT).parts[:-1]) & SKIP_DIRS) \
                and not SECRETISH.search(str(p.relative_to(ROOT))) and p.stat().st_size < 400_000:
            out.append(p)
    return sorted(out)


def _safe(path: str) -> Path:
    p = (ROOT / path.strip().lstrip("/\\")).resolve()
    if ROOT.resolve() not in p.parents and p != ROOT.resolve():
        raise ValueError("That's outside my own code.")
    rel = p.relative_to(ROOT.resolve())
    if set(rel.parts[:-1]) & SKIP_DIRS or SECRETISH.search(str(rel)):
        raise ValueError("I don't read that file (it isn't code, or it could hold secrets).")
    if not p.is_file():
        raise ValueError(f"There's no file {path} in my code. Use search_my_code to find the right one.")
    return p


def _servers() -> dict:
    try:
        return (yaml.safe_load((ROOT / "config" / "mcp_servers.yaml").read_text(encoding="utf-8")) or {}).get("mcpServers", {})
    except Exception:  # noqa: BLE001
        return {}


def _placement() -> tuple[list[str], list[str]]:
    try:
        from iriis.config import load_config
        cfg = load_config()
        return list(cfg.server.cloud_servers), list(cfg.desktop.servers)
    except Exception:  # noqa: BLE001
        return [], []


def _live() -> dict:
    port, key = os.environ.get("PORT", "8000"), os.environ.get("IRIIS_API_KEY", "")
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/v1/status", headers={"Authorization": f"Bearer {key}"}, timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        return {}


@mcp.tool()
def about_me() -> str:
    """How Iriis is set up right now: each of her skills, where it runs (cloud or the user's computer), whether it
    is working at this moment, what always needs the user's yes, the AI model, and which version is deployed.
    Use it for "what can you do / what do you have access to / are you connected"."""
    servers, (cloud, desk), live = _servers(), _placement(), _live()
    status = live.get("cloud_servers", {})
    lines = []
    for name, spec in servers.items():
        spec = spec or {}
        if spec.get("enabled") is False or (name not in cloud and name not in desk):
            continue
        where = WHERE["cloud"] if name in cloud else WHERE["desktop"]
        state = ""
        if name in cloud and name in status:
            state = "working" if status[name] == "ok" else f"NOT working ({status[name][:120]})"
        elif name in desk:
            state = "working" if live.get("desktop_connected") else "unavailable: the computer isn't connected"
        asks = sorted(k for k, v in (spec.get("tools") or {}).items() if v == "confirm")
        lines.append(f"- {name}: {spec.get('description', '').rstrip('.')}. Runs {where}." + (f" Now: {state}." if state else "")
                     + (f" Always asks first for: {', '.join(asks)}." if asks else ""))
    head = []
    if live:
        head.append(f"AI model: {live.get('llm', {}).get('model')}. Hearing: {live.get('stt')}. Voice: {live.get('tts')}.")
        head.append(f"Memory and records are stored in: {live.get('storage', '')} (the cloud database).")
        head.append(f"Computer link: {'connected' if live.get('desktop_connected') else 'not connected'}. "
                    f"Telegram: {'on' if live.get('telegram', {}).get('enabled') else 'off'}.")
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    if sha:
        head.append(f"Deployed version: {sha[:7]} \"{os.environ.get('RAILWAY_GIT_COMMIT_MESSAGE', '').splitlines()[0][:100]}\".")
    head.append("Safety: anything that sends, posts, buys, deletes, overwrites or runs risky commands needs the user's "
                "yes; background tasks can't get a yes, so they only prepare drafts.")
    return "\n".join(head) + "\n\nSkills:\n" + "\n".join(lines)


@mcp.tool()
def search_my_code(query: str, max_results: int = 20) -> str:
    """Search Iriis' own code, config and docs (the deployed version) for words or a phrase, e.g. "telegram",
    "reminder", "confirm". Returns file:line and the matching line. Then read_my_code for the details."""
    terms = [t for t in re.findall(r"\w{3,}", query.lower())] or [query.lower()]
    hits: list[tuple[int, str]] = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(text, 1):
            low = line.lower()
            score = sum(t in low for t in terms) + (2 if query.lower() in low else 0) + sum(t in rel.lower() for t in terms)
            if score >= max(1, len(terms) // 2 + 1) or query.lower() in low:
                hits.append((score, f"{rel}:{i}: {line.strip()[:160]}"))
    if not hits:
        return f"Nothing in my code mentions '{query}'."
    hits.sort(key=lambda h: -h[0])
    return "\n".join(h for _, h in hits[:max(1, min(max_results, 60))])


@mcp.tool()
def read_my_code(path: str, start_line: int = 1, lines: int = 150) -> str:
    """Read part of one of Iriis' own files (path as search_my_code shows it, e.g. "iriis/server/telegram.py")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8", errors="replace").splitlines()
    a = max(1, start_line)
    b = min(len(text), a + max(10, min(lines, 400)) - 1)
    body = "\n".join(f"{i:4d}  {text[i - 1]}" for i in range(a, b + 1))
    more = f"\n[... lines {b + 1}-{len(text)} not shown]" if b < len(text) else ""
    return f"{p.relative_to(ROOT).as_posix()} (lines {a}-{b} of {len(text)})\n{body}{more}"


@mcp.tool()
def list_my_files(folder: str = "") -> str:
    """The files that make up Iriis (optionally inside one folder, e.g. "servers" or "iriis/agent"), with a
    one-line summary of each Python module."""
    rows = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if folder and not rel.startswith(folder.strip("/").rstrip("/") + "/"):
            continue
        summary = ""
        if p.suffix == ".py":
            m = re.match(r'\s*(?:"""|\'\'\')(.+?)(?:\n|"""|\'\'\')', p.read_text(encoding="utf-8", errors="replace"), re.S)
            summary = m.group(1).strip()[:110] if m else ""
        rows.append(f"{rel}" + (f" - {summary}" if summary else ""))
    return "\n".join(rows[:300]) if rows else f"No files under {folder}."


@mcp.tool()
def recent_changes(count: int = 10) -> str:
    """The latest changes made to Iriis (from GitHub): when, and what each change was about."""
    try:
        r = httpx.get(f"https://api.github.com/repos/{REPO}/commits", params={"per_page": max(1, min(count, 30))},
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "iriis"}, timeout=15)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"Couldn't reach GitHub ({e.__class__.__name__})."
    out = []
    for c in r.json():
        msg = c["commit"]["message"].strip().splitlines()[0]
        out.append(f"{c['commit']['author']['date'][:10]} {c['sha'][:7]}: {msg}")
    live = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
    return "\n".join(out) + (f"\n(deployed right now: {live})" if live else "")


if __name__ == "__main__":
    mcp.run()
