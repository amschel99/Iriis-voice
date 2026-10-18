"""Tiny .env loader (no extra dependency) and URL helpers shared by the CLI and the desktop agent."""
from __future__ import annotations

import os
from pathlib import Path

from iriis.config import ROOT

ENV_PATH = ROOT / ".env"


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def load_env(path: Path = ENV_PATH) -> None:
    """Fill os.environ from .env without overriding variables that are already set."""
    for k, v in read_env(path).items():
        if v and not os.environ.get(k):
            os.environ[k] = v


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """Update or append keys, keeping comments and other lines intact."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={values[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def to_ws(url: str) -> str:
    url = url.rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[8:]
    if url.startswith("http://"):
        return "ws://" + url[7:]
    return url if url.startswith(("ws://", "wss://")) else "wss://" + url


def to_http(url: str) -> str:
    url = url.rstrip("/")
    if url.startswith("wss://"):
        return "https://" + url[6:]
    if url.startswith("ws://"):
        return "http://" + url[5:]
    return url if url.startswith(("http://", "https://")) else "https://" + url
