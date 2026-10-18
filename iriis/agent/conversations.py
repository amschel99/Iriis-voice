"""Persistent conversations: every exchange is saved, recent context is reloaded after a restart,
and older context is kept as a rolling summary. "Start over" adds a reset marker; nothing is
deleted, so past conversations stay searchable (see the memory server's search_conversations)."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time

from iriis.agent.memory import estimate_tokens
from iriis.storage import DB, get_db

OWNER_SESSION = "owner"   # the one ongoing conversation with Iriis' owner (voice + CLI share it)
log = logging.getLogger("iriis.conversations")
_INSERT = "INSERT INTO messages (session_id, role, content, created) VALUES (?, ?, ?, ?)"


class ConversationStore:
    """write_behind=True: append() only buffers in memory (instant); flush() writes the buffer to the
    database in one batch. A background task flushes every few minutes, and on shutdown (redeploys).
    Any read flushes first, so this process always sees its own latest messages."""

    MAX_PENDING = 200   # flush early if this many messages pile up

    def __init__(self, db: DB | None = None, write_behind: bool = False):
        self.db = db or get_db()
        self.write_behind = write_behind
        self._pending: list[tuple] = []
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()

    def append(self, session_id: str, role: str, content: str) -> None:
        row = (session_id, role, content, time.time())
        if not self.write_behind:
            self.db.execute(_INSERT, row)
            return
        with self._lock:
            self._pending.append(row)
            full = len(self._pending) >= self.MAX_PENDING
        if full:
            self.flush()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def flush(self) -> int:
        """Write buffered messages in one transaction. On failure they stay buffered for the next try."""
        with self._flush_lock:
            with self._lock:
                rows, self._pending = self._pending, []
            if not rows:
                return 0
            try:
                self.db.executemany(_INSERT, rows)
                return len(rows)
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._pending = rows + self._pending
                log.warning("conversation flush failed (%d kept for retry): %s", len(rows), e)
                return 0

    async def run_flusher(self, interval_s: float) -> None:
        """Background task: flush every interval_s seconds."""
        while True:
            await asyncio.sleep(interval_s)
            n = await asyncio.to_thread(self.flush)
            if n:
                log.info("saved %d conversation messages", n)

    def _since_reset(self, session_id: str, limit: int = 400) -> list[tuple]:
        return self.db.execute(
            "SELECT id, role, content, created FROM messages WHERE session_id = ? AND id > "
            "(SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ? AND role = 'reset') "
            "ORDER BY id DESC LIMIT ?", (session_id, session_id, limit))

    def recent(self, session_id: str, max_tokens: int) -> list[dict]:
        """Newest messages since the last reset that fit in max_tokens, oldest first, starting on a user turn."""
        self.flush()
        picked, used = [], 0
        for _id, role, content, _ in self._since_reset(session_id):
            msg = {"role": role, "content": content}
            used += estimate_tokens(msg)
            if used > max_tokens:
                break
            picked.append(msg)
        picked.reverse()
        while picked and picked[0]["role"] != "user":
            picked.pop(0)
        return picked

    def last_active(self, session_id: str) -> float | None:
        self.flush()
        rows = self.db.execute("SELECT MAX(created) FROM messages WHERE session_id = ? AND role != 'reset'", (session_id,))
        return float(rows[0][0]) if rows and rows[0][0] is not None else None

    def amend_last_reply(self, session_id: str, content: str) -> None:
        """Replace her latest reply in this conversation (e.g. with only the part the user heard)."""
        with self._lock:
            for i in range(len(self._pending) - 1, -1, -1):
                s, role, _, created = self._pending[i]
                if s == session_id and role == "assistant":
                    self._pending[i] = (s, role, content, created)
                    return
        self.db.execute("UPDATE messages SET content = ? WHERE id = (SELECT MAX(id) FROM messages "
                        "WHERE session_id = ? AND role = 'assistant')", (content, session_id))

    def reset(self, session_id: str) -> None:
        self.append(session_id, "reset", "")   # through the buffer, so ordering is kept
        self.db.execute("DELETE FROM conversation_summaries WHERE session_id = ?", (session_id,))

    def purge(self, session_id: str) -> None:
        """Really delete a conversation (DELETE /v1/sessions/{id}?purge=true)."""
        with self._lock:
            self._pending = [r for r in self._pending if r[0] != session_id]
        self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self.db.execute("DELETE FROM conversation_summaries WHERE session_id = ?", (session_id,))

    def summary(self, session_id: str) -> str:
        rows = self.db.execute("SELECT summary FROM conversation_summaries WHERE session_id = ?", (session_id,))
        return rows[0][0] if rows else ""

    def set_summary(self, session_id: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO conversation_summaries (session_id, summary, updated) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET summary = excluded.summary, updated = excluded.updated",
            (session_id, text, time.time()))

    def search(self, query: str = "", days: float | None = None, session_id: str | None = None,
               limit: int = 12) -> list[tuple[float, str, str, str]]:
        """Past messages matching any of the query's words (newest first): (created, session, role, content)."""
        self.flush()
        sql = "SELECT created, session_id, role, content FROM messages WHERE role IN ('user', 'assistant')"
        params: list = []
        if days:
            sql += " AND created >= ?"
            params.append(time.time() - days * 86400)
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2][:6]
        if words:
            sql += " AND (" + " OR ".join("LOWER(content) LIKE ?" for _ in words) + ")"
            params += [f"%{w}%" for w in words]
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [(float(c), s, r, t) for c, s, r, t in self.db.execute(sql, params)]


SUMMARY_PROMPT = """You maintain {name}'s long-term memory of an ongoing conversation with the user.
Update the summary with the new exchange below. Keep: what the user is working on, plans, decisions,
preferences, people/places mentioned, open questions and promises {name} made. Drop small talk and
tool output details. Write at most 180 words of plain prose. Reply with the summary only.

CURRENT SUMMARY:
{summary}

NEW EXCHANGE (older messages that no longer fit in context):
{dialogue}"""


def format_dialogue(messages: list[dict], max_chars: int = 6000) -> str:
    lines = [f"{m['role']}: {str(m.get('content') or '').strip()}" for m in messages
             if m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip()]
    return "\n".join(lines)[-max_chars:]
