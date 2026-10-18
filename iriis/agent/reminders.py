"""Polls the notes_reminders database and announces due reminders through the main process."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Awaitable, Callable

import time


class ReminderWatcher:
    def __init__(self, db_path: Path, announce: Callable[[str], Awaitable[None]], interval_s: float = 5.0):
        self.db_path = db_path
        self.announce = announce
        self.interval_s = interval_s

    def due(self) -> list[tuple[int, str, str]]:
        if not self.db_path.exists():
            return []
        try:
            with sqlite3.connect(self.db_path, timeout=5) as c:
                rows = list(c.execute("SELECT id, text, kind FROM reminders WHERE fired = 0 AND due <= ?", (time.time(),)))
                for rid, _, _ in rows:
                    c.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (rid,))
            return rows
        except sqlite3.OperationalError:
            return []  # table not created yet

    async def run(self) -> None:
        while True:
            for _, text, kind in await asyncio.to_thread(self.due):
                msg = f"Your {text} timer is done." if kind == "timer" else f"Reminder: {text}."
                await self.announce(msg)
            await asyncio.sleep(self.interval_s)
