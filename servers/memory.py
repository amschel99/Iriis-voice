"""memory MCP server: long-term facts the user explicitly asks to remember."""
from __future__ import annotations

from servers._common import data_dir, make_server
from iriis.agent.longterm import LongTermMemory

mcp = make_server("memory")
store = LongTermMemory(data_dir() / "memory.sqlite3")


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


if __name__ == "__main__":
    mcp.run()
