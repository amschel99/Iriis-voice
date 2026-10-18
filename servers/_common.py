"""Helpers shared by the built-in MCP servers."""
from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# mcp 2.x renamed FastMCP to MCPServer (same decorator API).
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class _Server(MCPServer):
    """MCPServer whose tools report their exception message instead of a masked generic error."""

    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*a, **kw):
                try:
                    return fn(*a, **kw)
                except ToolError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise ToolError(f"{e.__class__.__name__}: {e}") from e

            register(wrapper)
            return fn

        return decorator


def make_server(name: str) -> MCPServer:
    return _Server(name, log_level="WARNING")


def allowed_dirs() -> list[Path]:
    """Allowlisted directories, passed by Iriis in IRIIS_ALLOWED_DIRS (JSON list)."""
    raw = os.environ.get("IRIIS_ALLOWED_DIRS")
    dirs = json.loads(raw) if raw else ["~/Documents", "~/Desktop", "~/Downloads"]
    out = []
    for d in dirs:
        p = Path(os.path.expandvars(os.path.expanduser(d))).resolve()
        if p.exists():
            out.append(p)
    return out


def data_dir() -> Path:
    p = Path(os.environ.get("IRIIS_DATA_DIR", ROOT / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p
