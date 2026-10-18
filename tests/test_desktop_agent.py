"""The desktop agent must enforce its own tiers, whatever the cloud sends."""
import asyncio

from iriis.config import Config, MCPServerConfig
from iriis.desktop.agent import DesktopAgent
from iriis.ui.console import ConsoleUI


class FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args, confirmed=False):
        self.calls.append((name, args))
        return "ok: password=hunter2"


def make(tmp_path, mode="cloud"):
    cfg = Config()
    cfg.desktop.confirm_mode = mode
    cfg.permissions.audit_log = str(tmp_path / "audit.jsonl")
    agent = DesktopAgent(cfg, ConsoleUI(show_latency=False))
    agent.mcp = FakeMCP()
    agent.permissions.add_servers([
        MCPServerConfig(name="files", tier="auto", tools={"delete_file": "confirm", "format_disk": "blocked"}),
        MCPServerConfig(name="shell", tier="confirm"),
    ])
    return agent


def call(agent, name, args=None, confirmed=False):
    return asyncio.run(agent.handle_call({"type": "call", "id": "1", "name": name, "args": args or {}, "confirmed": confirmed}))


def test_auto_runs_and_output_is_redacted(tmp_path):
    agent = make(tmp_path)
    r = call(agent, "files__read_text_file", {"path": "a"})
    assert agent.mcp.calls and "hunter2" not in r["content"]


def test_blocked_refused_even_if_cloud_says_confirmed(tmp_path):
    agent = make(tmp_path)
    r = call(agent, "files__format_disk", confirmed=True)
    assert "BLOCKED" in r["error"] and not agent.mcp.calls
    r = call(agent, "shell__run_command", {"command": "rm -rf /"}, confirmed=True)
    assert "BLOCKED" in r["error"] and not agent.mcp.calls


def test_confirm_tier_needs_the_confirmed_flag(tmp_path):
    agent = make(tmp_path)
    r = call(agent, "files__delete_file", {"path": "a"}, confirmed=False)
    assert "DECLINED" in r["error"] and not agent.mcp.calls
    r = call(agent, "files__delete_file", {"path": "a"}, confirmed=True)
    assert not r["error"] and agent.mcp.calls


def test_shell_allowlist_applies_locally(tmp_path):
    agent = make(tmp_path)
    assert not call(agent, "shell__run_command", {"command": "git status"})["error"]
    assert "DECLINED" in call(agent, "shell__run_command", {"command": "git push"})["error"]


def test_local_mode_ignores_cloud_flag_and_asks_here(tmp_path, monkeypatch):
    agent = make(tmp_path, mode="local")
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    r = call(agent, "files__delete_file", {"path": "a"}, confirmed=True)
    assert "DECLINED" in r["error"] and not agent.mcp.calls
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes go ahead")
    r = call(agent, "files__delete_file", {"path": "a"}, confirmed=False)
    assert not r["error"] and agent.mcp.calls


def test_unknown_tool_defaults_to_confirm(tmp_path):
    agent = make(tmp_path)
    assert "DECLINED" in call(agent, "newthing__do", {})["error"]
