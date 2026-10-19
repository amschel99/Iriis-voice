"""Desktop agent: gives the cloud core hands on this computer, safely.

It runs the local MCP servers (files, shell, apps, clipboard, system info), connects OUT to the
cloud over a WebSocket (no open ports), and executes tool calls only after checking its own
permission tiers. The cloud cannot override them:
  blocked -> always refused here
  confirm -> confirm_mode "cloud": runs only if the call says you confirmed it in the cloud
             confirm_mode "local": asks you in this terminal, whatever the cloud says
  auto    -> runs
Every call is written to the local audit log.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Awaitable, Callable

from iriis.agent.permissions import AuditLog, PermissionManager, describe_call, is_affirmative, redact_secrets
from iriis.config import Config, resolve_path
from iriis.envfile import to_ws
from iriis.mcp_client.manager import MCPManager
from iriis.ui.console import ConsoleUI

log = logging.getLogger("iriis.desktop")
STATUS_FILE = resolve_path("data/desktop_status.json")

MSG_CONNECTED = "I'm connected to my brain and ready."
MSG_RECONNECTED = "I'm connected to my brain again."
MSG_LOST = "I've lost connection to my brain. I'll keep trying to reconnect."
MSG_NOT_CONNECTED = "I'm not connected to my brain right now. I'll keep trying."


def write_status(**fields) -> None:
    """Shared status file read by `iriis status` (merged, so the voice client can add its own keys)."""
    try:
        current = json.loads(STATUS_FILE.read_text(encoding="utf-8")) if STATUS_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    current.update(fields, pid=os.getpid(), updated=time.time())
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(current, indent=1), encoding="utf-8")


class DesktopAgent:
    def __init__(self, cfg: Config, ui: ConsoleUI):
        self.cfg, self.ui = cfg, ui
        self.mcp = MCPManager(cfg, only=cfg.desktop.servers)
        self.permissions = PermissionManager(cfg, [])
        self.audit = AuditLog(cfg.permissions.audit_log)
        self.ws = None
        self.connected = False
        self.on_announce: Callable[[str], Awaitable[None]] | None = None   # reminders from the cloud
        self.say_locally: Callable[[str], Awaitable[None]] | None = None   # on-device voice
        self._confirm_lock = asyncio.Lock()

    def _hello(self, kind: str = "hello") -> dict:
        servers = [s.model_dump() for s in self.mcp.server_configs if s.enabled and s.name in self.mcp.status()
                   and self.mcp.status()[s.name] == "ok"]
        return {"type": kind, "servers": servers, "catalog": self.mcp.servers_catalog(),
                "confirm_mode": self.cfg.desktop.confirm_mode}

    async def start_tools(self) -> None:
        problems = await self.mcp.start()
        self.permissions.servers = {}
        self.permissions.add_servers(self.mcp.server_configs)
        for p in problems:
            self.ui.warn(p)
        self.ui.info(f"Local tools: {len(self.mcp.all_tools())} from {', '.join(n for n, s in self.mcp.status().items() if s == 'ok')}")

    async def _ask_locally(self, tool: str, args: dict) -> bool:
        async with self._confirm_lock:
            self.ui.set_state("confirm")
            self.ui.assistant_sentence(describe_call(tool, args))
            self.ui._end_line()
            reply = await asyncio.to_thread(input, "confirm (yes/no)> ")
            return is_affirmative(reply, self.cfg.permissions.confirm_words)

    async def handle_call(self, msg: dict) -> dict:
        name, args = msg.get("name", ""), msg.get("args") or {}
        decision = self.permissions.decide(name, args)
        confirmed: bool | None = None
        base = {"type": "result", "id": msg.get("id")}
        if decision.tier == "blocked":
            self.audit.record(tool=name, args=args, tier="blocked", confirmed=None, executed=False, error=decision.reason)
            self.ui.show("tool", f"[blocked on this PC] {name}: {decision.reason}")
            return {**base, "error": f"BLOCKED by the desktop: {decision.reason}. Do not retry."}
        if decision.tier == "confirm":
            if self.cfg.desktop.confirm_mode == "local":
                confirmed = await self._ask_locally(name, args)
            else:
                confirmed = bool(msg.get("confirmed"))
            if not confirmed:
                self.audit.record(tool=name, args=args, tier="confirm", confirmed=False, executed=False)
                return {**base, "error": "DECLINED: this action needs the user's confirmation and did not get it."}
        if self.cfg.permissions.dry_run:
            self.audit.record(tool=name, args=args, tier=decision.tier, confirmed=confirmed, executed=False, dry_run=True)
            return {**base, "content": f"DRY RUN: {name} was not executed."}
        self.ui.show("tool", f"-> {name} {json.dumps(args, default=str)[:200]}")
        t0 = time.perf_counter()
        try:
            content, error = await self.mcp.call_tool(name, args), ""
        except Exception as e:  # noqa: BLE001
            content, error = "", f"{e.__class__.__name__}: {e}"
        content = redact_secrets(content)
        self.audit.record(tool=name, args=args, tier=decision.tier, confirmed=confirmed, executed=not error,
                          result=content, error=error, duration_ms=(time.perf_counter() - t0) * 1000)
        return {**base, "content": content[:8000], "error": error}

    async def _serve(self, ws) -> None:
        async def reply(msg):
            result = await self.handle_call(msg)
            await ws.send(json.dumps(result))

        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "call":
                asyncio.create_task(reply(msg))
            elif kind == "announce":
                self.ui.console.print(f"[bold yellow]⏰ {msg.get('text')}[/]")
                if self.on_announce:
                    await self.on_announce(msg.get("text", ""))
            elif kind == "reload":
                await self.mcp.reload()
                self.permissions.servers = {}
                self.permissions.add_servers(self.mcp.server_configs)
                await ws.send(json.dumps(self._hello("catalog")))

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        token = os.environ.get(self.cfg.desktop.token_env, "")
        base = to_ws(self.cfg.desktop.server_url)
        url = base + "/v1/desktop"
        if not token and not url.startswith(("ws://127.0.0.1", "ws://localhost")):
            raise SystemExit(f"Set {self.cfg.desktop.token_env} (run `iriis setup`).")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        delay, ever_connected, announced_down = 1.0, False, False
        write_status(brain_url=base, connected=False, since=time.time(), last_error="starting")
        while True:
            error = ""
            try:
                async with connect(url, additional_headers=headers, ping_interval=25, ping_timeout=60,
                                   max_size=16 * 1024 * 1024) as ws:
                    self.ws = ws
                    await ws.send(json.dumps(self._hello()))
                    self.connected = True
                    write_status(connected=True, since=time.time(), last_error="", tools=len(self.mcp.all_tools()))
                    self.ui.info(f"Connected to my brain at {base} (confirm mode: {self.cfg.desktop.confirm_mode})")
                    await self._say(MSG_RECONNECTED if ever_connected else MSG_CONNECTED)
                    ever_connected, announced_down, delay = True, False, 1.0
                    await self._serve(ws)
                    error = "the connection closed"
            except (OSError, asyncio.TimeoutError) as e:
                error = f"brain not reachable ({e.__class__.__name__})"
            except Exception as e:  # noqa: BLE001  (ConnectionClosed, InvalidStatus, ...)
                error = f"{e.__class__.__name__}: {e}"
                if "4401" in str(e) or "401" in str(e):
                    error = "the brain rejected my desktop token (check IRIIS_DESKTOP_TOKEN)"
            finally:
                self.ws = None
            was_connected, self.connected = self.connected, False
            write_status(connected=False, since=time.time(), last_error=error)
            self.ui.warn(f"Not connected: {error}; retrying in {delay:.0f} s")
            if not announced_down:
                await self._say(MSG_LOST if was_connected else MSG_NOT_CONNECTED)
                announced_down = True
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _say(self, text: str) -> None:
        if self.say_locally:
            await self.say_locally(text)

    async def stop(self) -> None:
        await self.mcp.stop()
