"""Connects to the configured MCP servers (stdio or streamable HTTP) and routes tool calls.

mcp 2.x API: `mcp.Client(StdioServerParameters | url)` as an async context manager.
anyio requires a client's context to be entered and exited in the same task, so each
server gets its own long-lived host task; tool calls may be made from any task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from iriis.config import ROOT, Config, MCPServerConfig, load_mcp_servers, resolve_path
from iriis.mcp_client.schema_convert import mcp_tool_to_llm, split_name

log = logging.getLogger("iriis.mcp")
CONNECT_TIMEOUT_S = 25.0
CALL_TIMEOUT_S = 60.0


@dataclass
class _Conn:
    cfg: MCPServerConfig
    client: Client | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    task: asyncio.Task | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    error: str = ""


class MCPManager:
    def __init__(self, config: Config, only: list[str] | None = None):
        self.config = config
        self.only = only  # restrict to these server names (cloud vs desktop split)
        self.conns: dict[str, _Conn] = {}
        self.server_configs: list[MCPServerConfig] = []

    # ------------------------------------------------------------------ lifecycle

    def _base_env(self) -> dict[str, str]:
        import os
        # The SDK starts servers with a minimal environment, so storage settings are passed explicitly.
        storage = {k: os.environ[k] for k in ("DATABASE_URL", "IRIIS_SQLITE_PATH") if os.environ.get(k)}
        return {**storage,
            "IRIIS_ALLOWED_DIRS": json.dumps(self.config.files.allowed_dirs),
            "IRIIS_DATA_DIR": str(resolve_path("data")),
            "IRIIS_TIMEZONE": self.config.timezone,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT),
            "HF_HUB_OFFLINE": "1",
            "DO_NOT_TRACK": "1",
        }

    def _target(self, cfg: MCPServerConfig) -> StdioServerParameters | str:
        if cfg.transport == "http":
            if not cfg.url:
                raise ValueError("http transport needs a url")
            host = urlparse(cfg.url).hostname
            if host not in ("127.0.0.1", "localhost", "::1") and not cfg.needs_internet:
                raise ValueError(f"{cfg.url} is not on localhost; set needs_internet: true to allow it")
            return cfg.url
        command = cfg.command or "python"
        if command in ("python", "python3", "${python}"):
            command = sys.executable  # built-in servers run in Iriis' own venv
        return StdioServerParameters(command=command, args=cfg.args, env={**self._base_env(), **cfg.env}, cwd=str(ROOT))

    async def _host(self, conn: _Conn) -> None:
        errlog = None
        try:
            target = self._target(conn.cfg)
            if isinstance(target, StdioServerParameters):
                # Keep server stderr (tracebacks, logs) out of the console UI.
                log_dir = resolve_path("logs/mcp")
                log_dir.mkdir(parents=True, exist_ok=True)
                errlog = open(log_dir / f"{conn.cfg.name}.log", "a", encoding="utf-8")
                target = stdio_client(target, errlog=errlog)
            async with Client(target, read_timeout_seconds=CALL_TIMEOUT_S, cache=None) as client:
                listing = await client.list_tools()
                conn.tools = [mcp_tool_to_llm(conn.cfg.name, t) for t in listing.tools]
                conn.client = client
                conn.ready.set()
                await conn.stop.wait()
        except Exception as e:  # noqa: BLE001
            conn.error = f"{e.__class__.__name__}: {e}"[:300]
            log.warning("MCP server %s failed: %s", conn.cfg.name, conn.error)
        finally:
            conn.client = None
            conn.ready.set()
            if errlog:
                errlog.close()

    async def start(self) -> list[str]:
        """Connect to every enabled server. Returns human-readable problems (empty if all good)."""
        self.server_configs = load_mcp_servers(self.config.mcp_config)
        if self.only is not None:
            self.server_configs = [s for s in self.server_configs if s.name in self.only]
        enabled = [s for s in self.server_configs if s.enabled]
        for cfg in enabled:
            conn = _Conn(cfg)
            self.conns[cfg.name] = conn
            conn.task = asyncio.create_task(self._host(conn), name=f"mcp:{cfg.name}")
        problems = []
        for name, conn in self.conns.items():
            try:
                await asyncio.wait_for(conn.ready.wait(), CONNECT_TIMEOUT_S)
            except asyncio.TimeoutError:
                conn.error = "timed out while starting"
            if conn.client is None:
                pretty = name.replace("_", " ")
                problems.append(f"The {pretty} tools aren't available right now.")
                log.warning("MCP %s unavailable: %s", name, conn.error)
        return problems

    async def stop(self) -> None:
        for conn in self.conns.values():
            conn.stop.set()
        tasks = [c.task for c in self.conns.values() if c.task]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for t in pending:
                t.cancel()
        self.conns.clear()

    async def reload(self) -> list[str]:
        await self.stop()
        return await self.start()

    # ------------------------------------------------------------------ queries

    def status(self) -> dict[str, str]:
        return {n: ("ok" if c.client else f"down: {c.error}") for n, c in self.conns.items()}

    def all_tools(self) -> list[dict[str, Any]]:
        return [t for c in self.conns.values() if c.client for t in c.tools]

    def servers_catalog(self) -> dict[str, dict[str, Any]]:
        return {
            n: {"description": c.cfg.description, "keywords": c.cfg.keywords, "tools": c.tools}
            for n, c in self.conns.items() if c.client
        }

    async def call_tool(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        server, tool = split_name(name)
        conn = self.conns.get(server)
        if not conn or not conn.client:
            raise RuntimeError(f"the {server} tools are not available")
        result = await asyncio.wait_for(conn.client.call_tool(tool, arguments), CALL_TIMEOUT_S + 5)
        parts = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(f"[{getattr(block, 'type', 'content')} omitted]")
        if not parts and result.structured_content is not None:
            parts.append(json.dumps(result.structured_content, default=str))
        text = "\n".join(parts) or "(no output)"
        return f"ERROR: {text}" if result.is_error else text
