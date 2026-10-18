"""Cloud side of the desktop link, plus a tool provider that merges cloud and desktop tools.

Protocol on WS /v1/desktop (JSON text frames):
  desktop -> cloud  {"type": "hello", "servers": [MCPServerConfig...], "catalog": {server: {description, keywords, tools}}, "confirm_mode": "cloud"|"local"}
  cloud -> desktop  {"type": "call", "id": "...", "name": "files__search_files", "args": {...}, "confirmed": bool}
  desktop -> cloud  {"type": "result", "id": "...", "content": "...", "error": ""}
  cloud -> desktop  {"type": "announce", "text": "..."}       (reminders etc.)
  cloud -> desktop  {"type": "reload"}                        (re-read local tool config)
  desktop -> cloud  {"type": "catalog", ...same as hello...}   (after a reload)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from iriis.agent.permissions import PermissionManager
from iriis.config import MCPServerConfig

log = logging.getLogger("iriis.desktop_bridge")
CALL_TIMEOUT_S = 75.0  # local confirmations can take a while


class DesktopBridge:
    def __init__(self, permissions: PermissionManager):
        self.permissions = permissions
        self.ws = None
        self.catalog: dict[str, dict[str, Any]] = {}
        self.server_names: list[str] = []
        self.confirm_mode = "cloud"
        self.pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.connected = asyncio.Event()

    @property
    def online(self) -> bool:
        return self.ws is not None

    def _register(self, msg: dict[str, Any]) -> None:
        self.permissions.remove_servers(self.server_names)
        servers = [MCPServerConfig.model_validate(s) for s in msg.get("servers", [])]
        self.confirm_mode = msg.get("confirm_mode", "cloud")
        if self.confirm_mode == "local":
            # The desktop asks in its own terminal; don't make the user confirm twice.
            for s in servers:
                s.tier = "auto" if s.tier == "confirm" else s.tier
                s.tools = {k: ("auto" if v == "confirm" else v) for k, v in s.tools.items()}
        self.permissions.add_servers(servers)
        self.server_names = [s.name for s in servers]
        self.catalog = msg.get("catalog", {})

    async def attach(self, ws) -> None:
        """Serve one desktop connection until it drops. `ws` is a Starlette WebSocket."""
        if self.ws is not None:
            await ws.close(code=4009, reason="another desktop agent is already connected")
            return
        hello = await ws.receive_json()
        if hello.get("type") != "hello":
            await ws.close(code=4400, reason="expected hello")
            return
        self.ws = ws
        self._register(hello)
        self.connected.set()
        n = sum(len(s["tools"]) for s in self.catalog.values())
        log.info("desktop connected with %d tools", n)
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "result":
                    fut = self.pending.pop(msg.get("id", ""), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif kind == "catalog":
                    self._register(msg)
        except Exception as e:  # noqa: BLE001 (WebSocketDisconnect and friends)
            log.info("desktop disconnected: %s", e.__class__.__name__)
        finally:
            self.ws = None
            self.connected.clear()
            self.permissions.remove_servers(self.server_names)
            self.catalog, self.server_names = {}, []
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("the desktop agent disconnected"))
            self.pending.clear()

    async def send(self, msg: dict[str, Any]) -> bool:
        if self.ws is None:
            return False
        async with self._send_lock:
            await self.ws.send_json(msg)
        return True

    def owns(self, tool: str) -> bool:
        return tool.partition("__")[0] in self.catalog

    async def call_tool(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        if self.ws is None:
            raise RuntimeError("your computer isn't connected right now")
        call_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = fut
        await self.send({"type": "call", "id": call_id, "name": name, "args": arguments, "confirmed": confirmed})
        try:
            msg = await asyncio.wait_for(fut, CALL_TIMEOUT_S)
        finally:
            self.pending.pop(call_id, None)
        if msg.get("error"):
            return f"ERROR: {msg['error']}"
        return msg.get("content", "")


class CompositeTools:
    """ToolProvider for the agent: cloud-hosted MCP servers + whatever the desktop offers."""

    def __init__(self, cloud, desktop: DesktopBridge | None):
        self.cloud = cloud      # MCPManager or None
        self.desktop = desktop

    def servers_catalog(self) -> dict[str, dict[str, Any]]:
        cat = dict(self.cloud.servers_catalog()) if self.cloud else {}
        if self.desktop and self.desktop.online:
            cat.update(self.desktop.catalog)
        return cat

    async def call_tool(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        if self.desktop and self.desktop.owns(name):
            return await self.desktop.call_tool(name, arguments, confirmed)
        if self.cloud:
            return await self.cloud.call_tool(name, arguments)
        raise RuntimeError(f"no provider for {name}")

    async def reload(self) -> list[str]:
        problems = await self.cloud.reload() if self.cloud else []
        if self.desktop and not await self.desktop.send({"type": "reload"}):
            problems.append("Your computer isn't connected, so its tools aren't available.")
        return problems
