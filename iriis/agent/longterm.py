"""Long-term memory: facts the user explicitly asks Iriis to remember (Postgres or SQLite)."""
from __future__ import annotations

import re
import time

from iriis.storage import DB, get_db


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class LongTermMemory:
    def __init__(self, db: DB | None = None):
        self.db = db or get_db()

    def save(self, text: str) -> int:
        return self.db.insert("INSERT INTO facts (text, created) VALUES (?, ?)", (text.strip(), time.time()))

    def all(self) -> list[tuple[int, str]]:
        return [(int(i), t) for i, t in self.db.execute("SELECT id, text FROM facts ORDER BY id")]

    def search(self, query: str, limit: int = 5) -> list[tuple[int, str]]:
        words = {w for w in _words(query) if len(w) > 2}
        scored = []
        for fid, text in self.all():
            score = len(words & _words(text))
            if score or not words:
                scored.append((score, fid, text))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [(fid, text) for _, fid, text in scored[:limit]]

    def forget(self, query_or_id: str) -> list[str]:
        if str(query_or_id).isdigit():
            targets = [(i, t) for i, t in self.all() if i == int(query_or_id)]
        else:
            q = _words(query_or_id)
            targets = [t for t in self.search(query_or_id, limit=3) if q & _words(t[1])][:1]
        for fid, _ in targets:
            self.db.execute("DELETE FROM facts WHERE id = ?", (fid,))
        return [t for _, t in targets]
