"""Telegram: text (or send voice notes to) Iriis from your phone, anywhere; task reports and reminders arrive there.

Free and official: create a bot with @BotFather, then set in Railway
    TELEGRAM_BOT_TOKEN   the token BotFather gives you
    TELEGRAM_CHAT_ID     your chat id (send your bot any message; it replies with the id to use)
Only the chat ids in TELEGRAM_CHAT_ID (comma-separated) are served; anyone else just gets told it's private.

Long polling (getUpdates), so no public webhook is needed. One conversation session ("telegram") keeps context.
When an action needs confirmation, she asks in the chat and your next message ("yes" / "no") answers it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("iriis.telegram")
API = "https://api.telegram.org"
MAX_LEN = 4000


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Telegram caps messages at 4096 characters: split on paragraph, then line, then space boundaries."""
    out, rest = [], text.strip()
    while len(rest) > limit:
        cut = next((c for c in (rest.rfind(sep, 0, limit) for sep in ("\n\n", "\n", " ")) if c > limit // 3), limit)
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return out + ([rest] if rest else [])


class TelegramBridge:
    def __init__(self, core, token: str | None = None, chat_ids: str | None = None,
                 run_turn: Callable[..., Awaitable[dict[str, Any]]] | None = None):
        self.core = core
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        raw = chat_ids if chat_ids is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.allowed = {c.strip() for c in raw.split(",") if c.strip()}
        self._run_turn = run_turn
        self.client: httpx.AsyncClient | None = None
        self.offset = 0
        self.turns: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _api(self, method: str, **params) -> Any:
        assert self.client is not None
        r = await self.client.post(f"{API}/bot{self.token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description', r.status_code)}")
        return data["result"]

    async def send(self, chat_id: str | int, text: str) -> None:
        for part in split_message(text):
            await self._api("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)

    async def broadcast(self, text: str) -> None:
        """Announcements and task reports go to every allowed chat."""
        if not (self.enabled and self.allowed and self.client):
            return
        for cid in self.allowed:
            try:
                await self.send(cid, text)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram send failed: %s", e)

    async def _voice_text(self, msg: dict[str, Any]) -> str:
        from iriis.server.speech import decode_audio_file
        f = await self._api("getFile", file_id=(msg.get("voice") or msg.get("audio"))["file_id"])
        r = await self.client.get(f"{API}/file/bot{self.token}/{f['file_path']}")
        audio = decode_audio_file(r.content)
        tr = await self.core.speech.transcribe(audio)
        return tr.text.strip()

    async def _turn(self, chat_id: int, text: str) -> None:
        from iriis.server.sessions import run_turn
        session = self.core.sessions.get("telegram")
        screens: list[str] = []

        async def emit(ev, _audio):
            if ev["type"] == "confirm":
                await self.send(chat_id, ev["question"] + "\n\n(Reply yes or no.)")
            elif ev["type"] == "screen":
                screens.append(ev["text"])

        try:
            await self._api("sendChatAction", chat_id=chat_id, action="typing")
            done = await (self._run_turn or run_turn)(session, text, emit, allow_confirm=True,
                                                      confirm_timeout=self.core.cfg.server.confirm_timeout_s * 3)
            reply = done["text"] + ("\n\n" + "\n".join(screens) if screens else "")
            if reply.strip() and reply.strip() != "(silent)":
                await self.send(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            log.exception("telegram turn failed")
            await self.send(chat_id, f"Sorry, that failed: {e.__class__.__name__}.")

    async def handle(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        if str(chat_id) not in self.allowed:
            hint = (f"This assistant is private. If you're the owner, set TELEGRAM_CHAT_ID={chat_id} in Railway."
                    if not self.allowed else "This assistant is private.")
            await self.send(chat_id, hint)
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text and (msg.get("voice") or msg.get("audio")):
            try:
                text = await self._voice_text(msg)
            except Exception as e:  # noqa: BLE001
                await self.send(chat_id, f"I couldn't hear that voice note ({e.__class__.__name__}).")
                return
            if text:
                await self.send(chat_id, f"(heard: {text})")
        if not text or text == "/start":
            await self.send(chat_id, "Hi, it's Iriis. Text me or send a voice note.")
            return
        session = self.core.sessions.get("telegram")
        if session.resolve_confirmation(None, text):    # the answer to "should I...?"
            return
        t = asyncio.create_task(self._turn(chat_id, text))
        self.turns.add(t)
        t.add_done_callback(self.turns.discard)

    async def run(self) -> None:
        if not self.enabled:
            return
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10))
        log.info("telegram: polling (%d allowed chat(s))", len(self.allowed))
        while True:
            try:
                updates = await self._api("getUpdates", offset=self.offset, timeout=50,
                                          allowed_updates=["message", "edited_message"])
                for u in updates:
                    self.offset = u["update_id"] + 1
                    await self.handle(u)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  (network blips, Telegram restarts)
                log.warning("telegram poll failed: %s", e)
                await asyncio.sleep(5)

    async def aclose(self) -> None:
        if self.client:
            await self.client.aclose()
