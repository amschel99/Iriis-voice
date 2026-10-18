"""Permission tiers, confirmation decisions, secret redaction and the audit log.

The tier of a tool call is decided ONLY from local config and the call's own arguments,
never from anything the LLM or a tool result says. Tool output can't escalate permissions.
"""
from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from iriis.config import Config, MCPServerConfig, Tier, resolve_path
from iriis.safety import shell_policy

_TIER_ORDER = {"auto": 0, "confirm": 1, "blocked": 2}

# Tool names (or globs) that are always blocked no matter what config says.
ALWAYS_BLOCKED = [
    "*__format_*", "*__wipe*", "*credential*", "*password*", "*keychain*", "*__read_secret*",
]


def stricter(a: Tier, b: Tier) -> Tier:
    return a if _TIER_ORDER[a] >= _TIER_ORDER[b] else b


@dataclass
class Decision:
    tier: Tier
    reason: str = ""


# Argument-aware classifiers: they may only make a call *stricter* than config, except the
# shell classifier, which may relax an allowlisted read-only command to "auto".
ArgClassifier = Callable[[dict[str, Any]], Decision | None]


def _shell_classifier(args: dict[str, Any]) -> Decision | None:
    cmd = str(args.get("command", ""))
    verdict, reason = shell_policy.classify(cmd)
    return Decision(verdict, reason)


class PermissionManager:
    def __init__(self, config: Config, servers: list[MCPServerConfig]):
        self.config = config
        self.servers = {s.name: s for s in servers}
        self.classifiers: dict[str, ArgClassifier] = {"shell__run_command": _shell_classifier}

    def add_servers(self, servers: list[MCPServerConfig]) -> None:
        """Register servers that live elsewhere (the desktop agent advertises its own tiers)."""
        for s in servers:
            self.servers[s.name] = s

    def remove_servers(self, names: list[str]) -> None:
        for n in names:
            self.servers.pop(n, None)

    def _config_tier(self, tool: str) -> Tier:
        perm = self.config.permissions
        for pattern in ALWAYS_BLOCKED:
            if fnmatch.fnmatch(tool.lower(), pattern):
                return "blocked"
        # 1. Global overrides in config.yaml (exact match beats glob).
        if tool in perm.overrides:
            return perm.overrides[tool]
        for pattern, tier in perm.overrides.items():
            if fnmatch.fnmatch(tool, pattern):
                return tier
        # 2. Server-level config in mcp_servers.yaml.
        server_name, _, bare = tool.partition("__")
        server = self.servers.get(server_name)
        if server is None:
            return perm.default_tier
        if bare in server.tools:
            return server.tools[bare]
        for pattern, tier in server.tools.items():
            if fnmatch.fnmatch(bare, pattern):
                return tier
        return server.tier

    def decide(self, tool: str, args: dict[str, Any]) -> Decision:
        tier = self._config_tier(tool)
        if tier == "blocked":
            return Decision("blocked", "blocked by configuration")
        classifier = self.classifiers.get(tool)
        if classifier:
            d = classifier(args)
            if d is not None:
                # The shell classifier is the one place a call can be relaxed (allowlisted
                # read-only commands run without asking). Everything else keeps the config tier.
                return d
        return Decision(tier, "")


# ---------------------------------------------------------------- secret redaction

_SECRET_PATTERNS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})"),               # OpenAI-style keys
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),            # GitHub tokens
    re.compile(r"\b(xox[abprs]-[A-Za-z0-9\-]{10,})"),         # Slack tokens
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),                     # AWS access key id
    re.compile(r"\b(AIza[0-9A-Za-z_\-]{30,})"),                # Google API key
    re.compile(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
# key=value / key: value anywhere in the text (not just at line start). Errs on the side of redacting.
_KV_SECRET = re.compile(
    r"(?i)(\b[A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|private[_-]?key|access[_-]?key)s?\b[\"']?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)


_SECRET_KEY = re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|apikey|private[_-]?key|access[_-]?key|credential)")


def redact_obj(obj: Any) -> Any:
    """Redact secrets inside tool-call arguments (dicts/lists), keeping the structure."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if _SECRET_KEY.search(str(k)) else redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_secrets(obj)
    return obj


def redact_secrets(text: str) -> str:
    if not text:
        return text
    text = _KV_SECRET.sub(lambda m: m.group(1) + "[REDACTED]", text)
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------- audit log

class AuditLog:
    def __init__(self, path: str, db=None, source: str = "local"):
        self.path = resolve_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = db          # optional iriis.storage.DB: entries are also written to its audit_log table
        self.source = source

    def record(self, *, tool: str, args: dict[str, Any], tier: Tier, confirmed: bool | None,
               executed: bool, result: str = "", error: str = "", dry_run: bool = False,
               duration_ms: float | None = None) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool,
            "args": redact_obj(json.loads(json.dumps(args, default=str))),
            "tier": tier,
            "confirmed": confirmed,
            "executed": executed,
            "dry_run": dry_run,
            "result_summary": redact_secrets(result[:300]),
            "error": error[:300],
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.db is not None:
            try:
                self.db.execute("INSERT INTO audit_log (ts, source, tool, tier, executed, entry) VALUES (?, ?, ?, ?, ?, ?)",
                                (entry["ts"], self.source, tool, tier, int(executed), line))
            except Exception:  # noqa: BLE001  (the JSONL file still has it)
                pass


def is_affirmative(utterance: str, confirm_words: list[str]) -> bool:
    """Strict yes-detection: the reply must contain a confirm word and no negation."""
    text = re.sub(r"[^a-z' ]", " ", utterance.lower()).strip()
    if not text:
        return False
    if re.search(r"\b(no|nope|don't|dont|do not|stop|cancel|wait|never|not)\b", text):
        return False
    padded = f" {text} "
    return any(f" {w} " in padded for w in confirm_words)


def describe_call(tool: str, args: dict[str, Any]) -> str:
    """A short spoken description of exactly what a tool call will do."""
    server, _, bare = tool.partition("__")
    action = bare.replace("_", " ") or tool
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 120:
            s = s[:117] + "..."
        parts.append(f"{k.replace('_', ' ')} {s}")
    detail = (", with " + "; ".join(parts)) if parts else ""
    return f"I'm about to {action} using the {server} tools{detail}. Shall I go ahead?"
