"""clipboard MCP server: read/write the system clipboard (pyperclip)."""
from __future__ import annotations

import pyperclip

from servers._common import make_server

mcp = make_server("clipboard")


@mcp.tool()
def read_clipboard() -> str:
    """Return the current text on the clipboard."""
    text = pyperclip.paste() or ""
    if not text:
        return "The clipboard is empty."
    return text[:4000] + ("\n[truncated]" if len(text) > 4000 else "")


@mcp.tool()
def write_clipboard(text: str) -> str:
    """Put text on the clipboard (replaces what's there)."""
    pyperclip.copy(text)
    return f"Copied {len(text)} characters to the clipboard."


if __name__ == "__main__":
    mcp.run()
