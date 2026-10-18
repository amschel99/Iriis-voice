"""Long-term memory: facts the user explicitly asks Iriis to remember (local SQLite)."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  created REAL NOT NULL
);
"""


class LongTermMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def save(self, text: str) -> int:
        text = text.strip()
        with self._conn() as c:
            cur = c.execute("INSERT INTO facts(text, created) VALUES (?, ?)", (text, time.time()))
            return int(cur.lastrowid)

    def all(self) -> list[tuple[int, str]]:
        with self._conn() as c:
            return list(c.execute("SELECT id, text FROM facts ORDER BY id"))

    def search(self, query: str, limit: int = 5) -> list[tuple[int, str]]:
        words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
        scored = []
        for fid, text in self.all():
            tw = set(re.findall(r"[a-z0-9]+", text.lower()))
            score = len(words & tw)
            if score or not words:
                scored.append((score, fid, text))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [(fid, text) for _, fid, text in scored[:limit]]

    def forget(self, query_or_id: str) -> list[str]:
        if str(query_or_id).isdigit():
            targets = [(int(query_or_id), t) for i, t in self.all() if i == int(query_or_id)]
        else:
            targets = [m for m in self.search(query_or_id, limit=3) if m]
            # only forget facts that actually match at least one word
            q = set(re.findall(r"[a-z0-9]+", query_or_id.lower()))
            targets = [t for t in targets if q & set(re.findall(r"[a-z0-9]+", t[1].lower()))][:1]
        with self._conn() as c:
            for fid, _ in targets:
                c.execute("DELETE FROM facts WHERE id = ?", (fid,))
        return [t for _, t in targets]
