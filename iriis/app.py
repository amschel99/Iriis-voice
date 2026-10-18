"""Wires the pipeline together: LLM, MCP tools, agent, and the text or voice front end."""
from __future__ import annotations

import asyncio
import logging
import sys

import psutil

from iriis.agent.longterm import LongTermMemory
from iriis.agent.loop import Agent, TurnCallbacks
from iriis.agent.permissions import PermissionManager
from iriis.agent.reminders import ReminderWatcher
from iriis.config import Config, resolve_path
from iriis.llm.base import LLMClient
from iriis.mcp_client.manager import MCPManager
from iriis.ui.console import ConsoleUI

log = logging.getLogger("iriis")


def make_llm(cfg: Config) -> LLMClient:
    if cfg.llm.backend == "openai":
        from iriis.llm.openai_client import OpenAICompatClient
        return OpenAICompatClient(cfg.llm)
    if cfg.llm.backend == "llamacpp":
        from iriis.llm.openai_client import LlamaCppClient
        return LlamaCppClient(cfg.llm)
    from iriis.llm.ollama_client import OllamaClient
    return OllamaClient(cfg.llm)


async def choose_model(cfg: Config, llm: LLMClient, ui: ConsoleUI) -> None:
    """Use the lighter fallback model when RAM is tight (unless the main model is already loaded)."""
    if cfg.llm.backend != "ollama" or not cfg.llm.fallback_model:
        return
    try:
        loaded = [m["name"] for m in (await llm.http.get("/api/ps")).json().get("models", [])]  # type: ignore[attr-defined]
    except Exception:
        loaded = []
    if cfg.llm.model in loaded:
        return
    free_gb = psutil.virtual_memory().available / 1024**3
    if free_gb < cfg.llm.use_fallback_below_free_gb:
        ui.warn(f"Only {free_gb:.1f} GB RAM free; using the lighter model {cfg.llm.fallback_model}.")
        llm.model = cfg.llm.fallback_model


class Iriis:
    def __init__(self, cfg: Config, use_tools: bool = True):
        self.cfg = cfg
        self.ui = ConsoleUI(cfg.ui.show_latency)
        self.llm = make_llm(cfg)
        self.mcp = MCPManager(cfg) if use_tools else None
        self.memory = LongTermMemory() if cfg.memory.enabled else None
        self.agent: Agent | None = None
        self.background: list[asyncio.Task] = []

    async def start(self) -> list[str]:
        """Connect everything. Returns spoken-style startup problems."""
        problems: list[str] = []
        ok, msg = await self.llm.health()
        (self.ui.info if ok else self.ui.error)(msg)
        if not ok:
            raise SystemExit(1)
        await choose_model(self.cfg, self.llm, self.ui)
        self.ui.info(f"LLM: {self.llm.model}")
        warm = asyncio.create_task(self.llm.warmup())
        servers = []
        if self.mcp:
            problems += await self.mcp.start()
            servers = self.mcp.server_configs
            for name, status in self.mcp.status().items():
                (self.ui.info if status == "ok" else self.ui.warn)(f"MCP {name}: {status}")
            self.ui.info(f"{len(self.mcp.all_tools())} tools available")
        perms = PermissionManager(self.cfg, servers)
        facts = (lambda: [t for _, t in self.memory.all()]) if self.memory else None
        from iriis.agent.conversations import ConversationStore
        self.agent = Agent(self.cfg, self.llm, self.mcp, perms, facts, store=ConversationStore())
        if self.cfg.permissions.dry_run:
            self.ui.warn("DRY RUN: tool calls are logged but not executed.")
        await warm
        return problems

    def start_reminders(self, announce) -> None:
        watcher = ReminderWatcher(announce)
        self.background.append(asyncio.create_task(watcher.run(), name="reminders"))

    async def stop(self) -> None:
        for t in self.background:
            t.cancel()
        if self.mcp:
            await self.mcp.stop()
        await self.llm.aclose()

    # ------------------------------------------------------------------ text mode

    async def run_text(self, once: str | None = None, speak: bool = False) -> None:
        ui = self.ui
        problems = await self.start()
        for p in problems:
            ui.warn(p)
        speaker = None
        if speak:
            from iriis.voice import Speaker
            speaker = Speaker(self.cfg, ui)
            await speaker.start()
        ui.banner(f"{self.cfg.assistant_name} (text mode). Type 'exit' to quit.")

        async def say(sentence: str) -> None:
            ui.assistant_sentence(sentence)
            if speaker:
                await speaker.say(sentence)

        async def confirm(question: str) -> str:
            ui.set_state("confirm")
            ui.assistant_sentence(question)
            ui._end_line()
            return await asyncio.to_thread(input, "confirm> ")

        async def announce(text: str) -> None:
            ui._end_line()
            ui.console.print(f"\n[bold yellow]⏰ {text}[/]")

        self.start_reminders(announce)
        cb = TurnCallbacks(say=say, confirm=confirm, show=ui.show)
        try:
            while True:
                if once is not None:
                    text = once
                else:
                    ui.set_state("idle")
                    try:
                        text = (await asyncio.to_thread(input, "you> ")).strip()
                    except EOFError:
                        break
                if not text:
                    continue
                if text.lower() in ("exit", "quit", "bye"):
                    break
                ui.set_state("thinking")
                try:
                    result = await self.agent.handle(text, cb)  # type: ignore[union-attr]
                except Exception as e:  # noqa: BLE001
                    ui.error(f"{e.__class__.__name__}: {e}")
                    log.exception("turn failed")
                    if once is not None:
                        raise
                    continue
                ui.latency(result.timings)
                if once is not None:
                    break
        finally:
            await self.stop()


def configure_logging(stdout: bool = False) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if stdout:  # containers: log to stdout so Railway shows it
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format=fmt)
    else:       # terminal UI: keep logs out of the console
        path = resolve_path("logs/iriis.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, filename=path, encoding="utf-8", format=fmt)
    for noisy in ("httpx", "httpcore", "mcp", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
