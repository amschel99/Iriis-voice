"""files MCP server. Every path must resolve inside an allowlisted directory.

Read tools: search_files, search_content, read_text_file, list_folder, open_file.
Write tools (configured as confirm-tier in mcp_servers.yaml): write_text_file, move_file, delete_file.
Secret files (.env, keys, credential stores) are refused outright.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from servers._common import allowed_dirs, make_server

mcp = make_server("files")

MAX_READ_CHARS = 8000
MAX_RESULTS = 25
SECRET_NAMES = re.compile(
    r"(^\.env(\..*)?$|\.pem$|\.key$|\.p12$|\.pfx$|\.kdbx$|^id_(rsa|ed25519|ecdsa|dsa)(\.pub)?$|credentials|secrets?\.(json|ya?ml|toml)$|\.netrc$|wallet\.dat$)",
    re.I,
)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "AppData", ".cache", "$RECYCLE.BIN"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".html", ".css", ".log", ".ini", ".toml", ".xml", ".rst", ".tex", ".sh", ".ps1", ".bat", ".sql", ".java", ".c", ".cpp", ".h", ".go", ".rs"}


class AccessDenied(ToolError):
    """ToolError messages are passed back to the client instead of being masked."""


def _resolve(path: str) -> Path:
    roots = allowed_dirs()
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_absolute():
        # relative paths are tried against each allowed root
        for r in roots:
            if (r / p).exists():
                p = r / p
                break
        else:
            p = (roots[0] / p) if roots else p
    p = p.resolve()
    if not any(p == r or r in p.parents for r in roots):
        raise AccessDenied(f"{p} is outside the allowed folders: {', '.join(str(r) for r in roots)}")
    if SECRET_NAMES.search(p.name):
        raise AccessDenied(f"{p.name} looks like a secrets file; access is blocked.")
    return p


def _walk(roots: list[Path]):
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for f in filenames:
                yield Path(dirpath) / f


@mcp.tool()
def allowed_folders() -> str:
    """List the folders Iriis is allowed to access."""
    return "\n".join(str(r) for r in allowed_dirs()) or "No allowed folders configured."


@mcp.tool()
def search_files(pattern: str, folder: str = "") -> str:
    """Find files by name. pattern may be a word ('invoice') or a glob ('*.pdf')."""
    roots = [_resolve(folder)] if folder else allowed_dirs()
    glob = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
    hits = []
    for f in _walk(roots):
        if fnmatch.fnmatch(f.name.lower(), glob.lower()) and not SECRET_NAMES.search(f.name):
            hits.append(f)
            if len(hits) >= MAX_RESULTS:
                break
    if not hits:
        return f"No files matching '{pattern}'."
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return "\n".join(str(h) for h in hits)


@mcp.tool()
def search_content(text: str, folder: str = "", extension: str = "") -> str:
    """Find text files containing some text (case-insensitive). Optional extension filter like '.md'."""
    roots = [_resolve(folder)] if folder else allowed_dirs()
    needle = text.lower()
    hits = []
    for f in _walk(roots):
        if (extension and f.suffix.lower() != extension.lower()) or f.suffix.lower() not in TEXT_EXT:
            continue
        if SECRET_NAMES.search(f.name):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            with f.open("r", encoding="utf-8", errors="ignore") as fh:
                for n, line in enumerate(fh, 1):
                    if needle in line.lower():
                        hits.append(f"{f}:{n}: {line.strip()[:120]}")
                        break
        except OSError:
            continue
        if len(hits) >= MAX_RESULTS:
            break
    return "\n".join(hits) or f"No files contain '{text}'."


@mcp.tool()
def read_text_file(path: str) -> str:
    """Read a text file (first 8000 characters)."""
    p = _resolve(path)
    if not p.is_file():
        return f"Not a file: {p}"
    data = p.read_text(encoding="utf-8", errors="replace")
    suffix = f"\n[truncated, {len(data)} characters total]" if len(data) > MAX_READ_CHARS else ""
    return data[:MAX_READ_CHARS] + suffix


@mcp.tool()
def list_folder(path: str = "") -> str:
    """List the contents of a folder (defaults to the allowed folders)."""
    if not path:
        return allowed_folders()
    p = _resolve(path)
    if not p.is_dir():
        return f"Not a folder: {p}"
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = [f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in entries[:100] if not SECRET_NAMES.search(e.name)]
    more = f"\n... and {len(entries) - 100} more" if len(entries) > 100 else ""
    return f"{p}:\n" + "\n".join(lines) + more


@mcp.tool()
def open_file(path: str) -> str:
    """Open a file or folder with its default application."""
    p = _resolve(path)
    if not p.exists():
        return f"Does not exist: {p}"
    if sys.platform == "win32":
        os.startfile(str(p))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return f"Opened {p.name}"


# ---------------------------------------------------------------- write tools (confirm tier)

@mcp.tool()
def write_text_file(path: str, content: str, overwrite: bool = False) -> str:
    """Create (or overwrite, if overwrite=true) a text file inside an allowed folder."""
    p = _resolve(path)
    if p.exists() and not overwrite:
        return f"{p} already exists; pass overwrite=true to replace it."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {p}"


@mcp.tool()
def move_file(source: str, destination: str) -> str:
    """Move or rename a file/folder; both locations must be inside allowed folders."""
    src, dst = _resolve(source), _resolve(destination)
    if not src.exists():
        return f"Does not exist: {src}"
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        return f"Destination already exists: {dst}"
    shutil.move(str(src), str(dst))
    return f"Moved {src} to {dst}"


@mcp.tool()
def delete_file(path: str) -> str:
    """Delete a single file (not folders) inside an allowed folder. Sends it to a .iriis_trash folder."""
    p = _resolve(path)
    if not p.is_file():
        return f"Not a file: {p}"
    root = next(r for r in allowed_dirs() if r == p or r in p.parents)
    trash = root / ".iriis_trash"
    trash.mkdir(exist_ok=True)
    shutil.move(str(p), str(trash / p.name))
    return f"Moved {p.name} to {trash} (recoverable)."


if __name__ == "__main__":
    mcp.run()
