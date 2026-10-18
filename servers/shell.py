"""shell MCP server.

Iriis' permission manager classifies each command with iriis.safety.shell_policy:
allowlisted read-only commands run straight away, anything else needs spoken confirmation,
and blocked patterns never run. This server re-checks the blocklist itself (defence in depth),
and enforces a timeout and an output size limit.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from servers._common import make_server
from iriis.safety.shell_policy import classify

mcp = make_server("shell")

TIMEOUT_S = float(os.environ.get("IRIIS_SHELL_TIMEOUT", "20"))
MAX_OUTPUT = 6000


GIT_BASH = [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]


def _argv(command: str, shell: str = "") -> list[str]:
    shell = (shell or ("powershell" if sys.platform == "win32" else "bash")).lower()
    if shell in ("bash", "git bash", "gitbash", "sh"):
        if sys.platform == "win32":
            bash = next((b for b in GIT_BASH if Path(b).exists()), None) or shutil.which("bash")
            if not bash:
                raise FileNotFoundError("Git Bash isn't installed")
            return [bash, "-lc", command]
        return ["/bin/bash", "-lc", command]
    if shell == "cmd" and sys.platform == "win32":
        return ["cmd.exe", "/d", "/c", command]
    if sys.platform == "win32":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-c", command]


@mcp.tool()
def run_command(command: str, working_directory: str = "", shell: str = "") -> str:
    """Run a terminal command and return its output. shell: "powershell" (default on Windows), "bash" (Git Bash)
    or "cmd". Read-only commands like 'dir', 'ls', 'git status', 'ping -n 2 example.com' run directly;
    anything else needs the user's spoken confirmation."""
    verdict, reason = classify(command)
    if verdict == "blocked":
        return f"BLOCKED: {reason}. This command is never run."
    cwd = Path(os.path.expanduser(working_directory or "~"))
    if not cwd.is_dir():
        return f"Working directory does not exist: {cwd}"
    try:
        proc = subprocess.run(
            _argv(command, shell), cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S,
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
