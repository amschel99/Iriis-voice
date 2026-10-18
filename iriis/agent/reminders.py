"""Polls the reminders table and announces due reminders through the main process."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from iriis.storage import DB, get_db

log = logging.getLogger("iriis.reminders")


class ReminderWatcher:
    def __init__(self, announce: Callable[[str], Awaitable[None]], db: DB | None = None, interval_s: float = 5.0):
        self.db = db
        self.announce = announce
        self.interval_s = interval_s

    def due(self) -> list[tuple[int, str, str]]:
        """Claim due reminders atomically (safe even if two processes poll the same database)."""
        db = self.db or get_db()
        return [tuple(r) for r in db.execute(
            "UPDATE reminders SET fired = 1 WHERE fired = 0 AND due <= ? RETURNING id, text, kind", (time.time(),))]

    async def run(self) -> None:
        while True:
            try:
                rows = await asyncio.to_thread(self.due)
            except Exception as e:  # noqa: BLE001  (database briefly unreachable)
                log.warning("reminder poll failed: %s", e)
                rows = []
            for _, text, kind in rows:
                msg = f"Your {text} timer is done." if kind == "timer" else f"Reminder: {text}."
                await self.announce(msg)
            await asyncio.sleep(self.interval_s)
