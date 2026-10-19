"""When the router didn't offer a tool, she can fetch it mid-turn (more_tools) instead of failing."""
from __future__ import annotations

import asyncio

from iriis.agent.loop import MORE_TOOLS_NAME, Agent, TurnCallbacks
from iriis.agent.permissions import PermissionManager
from iriis.config import Config, MCPServerConfig
from iriis.llm.base import LLMClient, LLMEvent, ToolCall


def _tool(name, desc):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": {}}}}


class Provider:
    """A big catalog: shell is NOT matched by the words 'whats using port 8000'."""
    def __init__(self):
        self.calls = []
        self.cat = {f"filler{i}": {"description": "misc", "keywords": [], "tools": [_tool(f"filler{i}__t{j}", "x") for j in range(10)]}
                    for i in range(8)}
        self.cat["shell"] = {"description": "Run terminal commands", "keywords": ["terminal", "command", "shell"],
                             "tools": [_tool("shell__run_command", "Run a terminal command")]}

    def servers_catalog(self):
        return self.cat

    async def call_tool(self, name, args, confirmed=False):
        self.calls.append(name)
        return "TCP 0.0.0.0:8000 LISTENING python.exe"


class Script(LLMClient):
    def __init__(self):
        self.model, self.step, self.seen = "fake", 0, []

    async def chat_stream(self, messages, tools=None, model=None):
        names = {t["function"]["name"] for t in (tools or [])}
        self.seen.append(names)
        self.step += 1
        if self.step == 1:
            assert "shell__run_command" not in names and MORE_TOOLS_NAME in names
            yield LLMEvent(done=True, tool_calls=[ToolCall(MORE_TOOLS_NAME, {"need": "run a terminal command"}, "a")])
        elif self.step == 2:
            assert "shell__run_command" in names
            yield LLMEvent(done=True, tool_calls=[ToolCall("shell__run_command", {"command": "netstat"}, "b")])
        else:
            yield LLMEvent(text="Python is using port 8000.")
            yield LLMEvent(done=True)

    async def health(self):
        return True, "ok"


def test_fetches_a_missing_tool_and_uses_it(tmp_path):
    cfg = Config()
    cfg.tool_routing.max_tools = 20
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    prov = Provider()
    pm = PermissionManager(cfg, [MCPServerConfig(name="shell", tier="auto")])
    agent = Agent(cfg, Script(), prov, pm)
    agent.router.max_tools = 20

    async def say(_):
        pass

    async def nop(*_):
        return "yes"
    r = asyncio.run(agent.handle("whats using port 8000", TurnCallbacks(say=say, confirm=nop)))
    assert prov.calls == ["shell__run_command"] and "port 8000" in r.text


def test_a_cut_off_tool_call_is_explained_not_run():
    """The model ran out of room mid-call: the tool must not run with empty arguments, and she's told why."""
    import asyncio
    from types import SimpleNamespace
    from iriis.agent.loop import Agent
    from iriis.llm.base import ToolCall
    agent = Agent.__new__(Agent)
    agent.tools = SimpleNamespace(call_tool=None)
    out = asyncio.run(agent._execute(ToolCall("studio__make_document", {"_raw": '{"kind": "pdf", "content": "# Gui'}, "c1"),
                                     {"studio__make_document"}, None))
    assert out.startswith("ERROR: your call to this tool was cut off")
