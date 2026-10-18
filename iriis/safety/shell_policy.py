"""Shell command policy shared by the permission manager (client) and the shell MCP server.

classify(cmd) -> ("auto" | "confirm" | "blocked", reason)
  auto     every segment is an allowlisted read-only command
  confirm  anything else that isn't blocked
  blocked  matches a destructive / remote-code pattern; never runs, even if confirmed
"""
from __future__ import annotations

import re
import shlex

# First word (lower-cased, without .exe) -> optional predicate on the remaining args.
# None means any arguments are fine.
READ_ONLY: dict[str, object] = {
    "ls": None, "dir": None, "pwd": None, "cd": None, "echo": None, "whoami": None, "hostname": None,
    "date": None, "uptime": None, "df": None, "du": None, "free": None, "cat": None, "type": None,
    "head": None, "tail": None, "wc": None, "which": None, "where": None, "uname": None,
    "ping": None, "ipconfig": None, "ifconfig": None, "ip": None, "nslookup": None, "tracert": None,
    "traceroute": None, "netstat": None, "systeminfo": None, "tasklist": None, "ps": None, "top": None,
    "ver": None, "python": "version", "python3": "version", "node": "version", "nvidia-smi": None,
    "git": "git", "find": "find", "grep": None, "findstr": None, "tree": None,
    "get-childitem": None, "get-process": None, "get-date": None, "get-content": None, "get-location": None,
    "ollama": "ollama",
}

_GIT_READ_ONLY = {"status", "log", "diff", "branch", "show", "remote", "rev-parse", "describe", "tag", "blame", "shortlog", "ls-files"}
_OLLAMA_READ_ONLY = {"list", "ps", "show", "--version", "-v"}

BLOCKED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)[a-z]*\s+(/|~|\*|/\*|\$home|c:\\?)(\s|$)", re.I), "recursive delete of a root or home directory"),
    (re.compile(r"\brm\s+.*--no-preserve-root", re.I), "rm --no-preserve-root"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "formatting a filesystem"),
    (re.compile(r"(^|[\s;&|])format(\.com)?\s+[a-z]:", re.I), "formatting a drive"),
    (re.compile(r"\bformat-volume\b|\bclear-disk\b|\binitialize-disk\b|\bdiskpart\b", re.I), "disk partitioning/formatting"),
    (re.compile(r"\bdd\s+.*\bof=/dev/", re.I), "raw write to a device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd)", re.I), "raw write to a device"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", re.I), "fork bomb"),
    (re.compile(r"\b(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b.*\|\s*(sudo\s+)?(ba|z|da)?sh\b", re.I), "piping a remote script to a shell"),
    (re.compile(r"\b(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b.*\|\s*(iex|invoke-expression|python|pwsh|powershell)\b", re.I), "piping a remote script to an interpreter"),
    (re.compile(r"\b(iex|invoke-expression)\s*\(?\s*\(?\s*(new-object\s+net\.webclient|iwr|irm|invoke-webrequest)", re.I), "executing downloaded code"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\s+/(\s|$)", re.I), "chmod 777 on root"),
    (re.compile(r"\bchown\s+-R\s+.*\s/(\s|$)", re.I), "recursive chown of root"),
    (re.compile(r"\b(reg(\.exe)?\s+delete|remove-item\s+.*hk(lm|cu):)", re.I), "deleting registry keys"),
    (re.compile(r"\bbcdedit\b|\bvssadmin\s+delete\b|\bwbadmin\s+delete\b", re.I), "boot/backup tampering"),
    (re.compile(r"\bcipher\s+/w\b|\bsdelete\b|\bshred\b", re.I), "secure-wiping data"),
    (re.compile(r"\b(rd|rmdir)\s+/s\s+(/q\s+)?[a-z]:\\?(\s|$)", re.I), "deleting a whole drive"),
    (re.compile(r"\bdel\s+/[a-z\s/]*\s+[a-z]:\\\*?(\s|$)", re.I), "deleting a whole drive"),
    (re.compile(r"\bremove-item\s+.*-recurse.*\s[a-z]:\\?(\s|$)", re.I), "deleting a whole drive"),
    (re.compile(r"(\.ssh[/\\]id_|\.aws[/\\]credentials|\.netrc|/etc/shadow|\\config\\sam\b|mimikatz|lsass|cmdkey\s+/list|vaultcmd)", re.I), "credential access"),
    (re.compile(r"(^|[\s/\\\"'])\.env(\.[\w]+)?([\s\"']|$)", re.I), "reading secrets from a .env file"),
    (re.compile(r"\b(net\s+user\s+\S+\s+\S+|passwd)\b", re.I), "changing account passwords"),
]

# Destructive but legitimate: never auto, always confirmed (handled by falling through to "confirm").
_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_REDIRECT = re.compile(r"(^|[^0-9&])>{1,2}(?!&)")


def _check_blocked(cmd: str) -> str | None:
    for pat, reason in BLOCKED_PATTERNS:
        if pat.search(cmd):
            return reason
    # shutdown/restart only via the apps_and_media tool (which asks), never via raw shell auto.
    return None


def _first_word(segment: str) -> tuple[str, list[str]]:
    try:
        parts = shlex.split(segment, posix=True)
    except ValueError:
        parts = segment.split()
    if not parts:
        return "", []
    head = parts[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if head.endswith(".exe"):
        head = head[:-4]
    return head, parts[1:]


def _segment_read_only(segment: str) -> bool:
    head, rest = _first_word(segment)
    if head not in READ_ONLY:
        return False
    rule = READ_ONLY[head]
    if rule is None:
        return True
    if rule == "version":
        return rest in (["--version"], ["-V"], ["-v"])
    if rule == "git":
        sub = next((a for a in rest if not a.startswith("-")), "")
        if sub == "branch" and any(a in ("-d", "-D", "--delete", "-m", "-M") for a in rest):
            return False
        if sub in ("remote", "tag") and len([a for a in rest if not a.startswith("-")]) > 1:
            return False  # `git remote add`, `git tag v1` modify state
        return sub in _GIT_READ_ONLY
    if rule == "ollama":
        return bool(rest) and rest[0] in _OLLAMA_READ_ONLY
    if rule == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for a in rest)
    return False


def classify(cmd: str) -> tuple[str, str]:
    cmd = (cmd or "").strip()
    if not cmd:
        return "blocked", "empty command"
    reason = _check_blocked(cmd)
    if reason:
        return "blocked", reason
    if _REDIRECT.search(cmd) or "`" in cmd or "$(" in cmd:
        return "confirm", "uses redirection or command substitution"
    segments = [s for s in _SEGMENT_SPLIT.split(cmd) if s]
    if segments and all(_segment_read_only(s) for s in segments):
        return "auto", "allowlisted read-only command"
    return "confirm", "not on the read-only allowlist"
