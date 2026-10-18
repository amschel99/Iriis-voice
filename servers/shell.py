"""shell MCP server.

Iriis' permission manager classifies each command with iriis.safety.shell_policy:
allowlisted read-only commands run straight away, anything else needs spoken confirmation,
and blocked patterns never run. This server re-checks the blocklist itself (defence in depth),
and enforces a timeout and an output size limit.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from servers._common import make_server
from iriis.safety.shell_policy import classify

mcp = make_server("shell")

TIMEOUT_S = float(os.environ.get("IRIIS_SHELL_TIMEOUT", "20"))
MAX_OUTPUT = 6000


def _argv(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-c", command]


@mcp.tool()
def run_command(command: str, working_directory: str = "") -> str:
    """Run a shell command (PowerShell on Windows, sh elsewhere) and return its output.
    Read-only commands like 'dir', 'git status', 'ping -n 2 example.com' run directly; others need the user's confirmation."""
    verdict, reason = classify(command)
    if verdict == "blocked":
        return f"BLOCKED: {reason}. This command is never run."
    cwd = Path(os.path.expanduser(working_directory or "~"))
    if not cwd.is_dir():
        return f"Working directory does not exist: {cwd}"
    try:
        proc = subprocess.run(
            _argv(command), cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {TIMEOUT_S:.0f} seconds."
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
    out = out.strip() or "(no output)"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n[output truncated, {len(out)} characters total]"
    return f"exit code {proc.returncode}\n{out}"


if __name__ == "__main__":
    mcp.run()
