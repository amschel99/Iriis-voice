"""memory MCP server: long-term facts the user explicitly asks to remember (Postgres or SQLite)."""
from __future__ import annotations

import os

from servers._common import make_server
from iriis.agent.conversations import ConversationStore
from iriis.agent.longterm import LongTermMemory

mcp = make_server("memory")
store = LongTermMemory()
conversations = ConversationStore()  # Postgres if DATABASE_URL is set, else data/iriis.sqlite3


@mcp.tool()
def remember(fact: str) -> str:
    """Save a fact the user explicitly asked you to remember, e.g. 'My gym is at Sarit Centre'."""
    fid = store.save(fact)
    return f"Saved memory #{fid}: {fact}"


@mcp.tool()
def recall(query: str = "") -> str:
    """Look up saved memories relevant to a query (empty query lists recent ones)."""
    hits = store.search(query, limit=8) if query else store.all()[-8:]
    return "\n".join(f"#{i}: {t}" for i, t in hits) or "Nothing saved about that."


@mcp.tool()
def forget(query: str) -> str:
    """Delete the saved memory that best matches the query (or a memory id number)."""
    removed = store.forget(query)
    return ("Forgot: " + "; ".join(removed)) if removed else "No matching memory to forget."


@mcp.tool()
def search_conversations(query: str = "", days: float = 30, limit: int = 12) -> str:
    """Search past conversations with the user (what they said and what you replied), newest first.
    Use for "what did I tell you about X", "what were we talking about yesterday". Empty query = most recent messages."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(os.environ.get("IRIIS_TIMEZONE", "Africa/Nairobi"))
    hits = conversations.search(query, days=days or None, limit=max(1, min(limit, 40)))
    if not hits:
        return f"No past conversation matches '{query}' in the last {days:g} days." if query else "No past conversations yet."
    lines = []
    for created, _session, role, content in hits:
        when = dt.datetime.fromtimestamp(created, tz).strftime("%a %d %b %H:%M")
        who = "user" if role == "user" else "you"
        lines.append(f"[{when}] {who}: {content[:300]}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
