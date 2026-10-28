"""Skills: the matching SKILL.md is loaded into her prompt with its tools; use_skill opens one mid-turn."""
from __future__ import annotations

import asyncio

import pytest

from iriis.agent.loop import Agent, TurnCallbacks
from iriis.agent.permissions import PermissionManager
from iriis.agent.skills import SKILLS_DIR, USE_SKILL_NAME, SkillLibrary, parse
from iriis.config import Config, MCPServerConfig
from iriis.llm.base import LLMClient, LLMEvent, ToolCall
from servers import research


def test_every_shipped_skill_parses_and_names_real_servers():
    lib = SkillLibrary()
    assert {"community-pulse", "deep-research", "research-papers", "long-task-planning", "web-browsing"} <= set(lib.skills)
    import yaml
    servers = set(yaml.safe_load((SKILLS_DIR.parent / "config" / "mcp_servers.yaml").read_text(encoding="utf-8"))["mcpServers"])
    for s in lib.skills.values():
        assert s.description and len(s.body) > 400, s.name
        assert set(s.servers) <= servers, (s.name, s.servers)


@pytest.mark.parametrize("ask,skill", [
    ("what are people saying about DeepSeek 4 this month", "community-pulse"),
    ("find the best papers on agent memory and explain arxiv 2304.03442", "research-papers"),
    ("do a deep research report comparing solar freezer suppliers in Nairobi", "deep-research"),
    ("open whatsapp web and dm Kevin that I'm late", "web-browsing"),
])
def test_requests_pick_the_right_skill(ask, skill):
    assert SkillLibrary().match(ask)[0].name == skill


def test_small_talk_loads_no_skill():
    assert SkillLibrary().match("hey how are you doing today") == []


def test_parse_rejects_files_without_front_matter():
    with pytest.raises(ValueError):
        parse("# just a title")


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}


class Provider:
    def __init__(self):
        self.cat = {f"filler{i}": {"description": "misc", "keywords": [], "tools": [_tool(f"filler{i}__t{j}") for j in range(10)]}
                    for i in range(8)}
        self.cat["research"] = {"description": "papers", "keywords": [], "tools": [_tool("research__community_pulse")]}

    def servers_catalog(self):
        return self.cat

    async def call_tool(self, name, args, confirmed=False):
        return "ok"


class Script(LLMClient):
    def __init__(self, first_call):
        self.model, self.step, self.seen, self.first = "fake", 0, [], first_call

    async def chat_stream(self, messages, tools=None, model=None):
        self.seen.append(({t["function"]["name"] for t in (tools or [])}, messages[0]["content"]))
        self.step += 1
        if self.step == 1 and self.first:
            yield LLMEvent(done=True, tool_calls=[self.first])
        else:
            yield LLMEvent(text="Done.")
            yield LLMEvent(done=True)

    async def health(self):
        return True, "ok"


def _agent(tmp_path, llm):
    cfg = Config()
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    agent = Agent(cfg, llm, Provider(), PermissionManager(cfg, [MCPServerConfig(name="research", tier="auto")]))
    agent.router.max_tools = 20
    return agent


async def _yes(*_):
    return "yes"


async def _say(_):
    pass


def test_a_matched_skill_is_in_the_prompt_with_its_tools(tmp_path):
    llm = Script(None)
    asyncio.run(_agent(tmp_path, llm).handle("what are people on reddit saying about ntfy", TurnCallbacks(say=_say, confirm=_yes)))
    names, system = llm.seen[0]
    assert '<skill name="community-pulse">' in system and "research__community_pulse" in names
    assert USE_SKILL_NAME in names and "Your skills" in system


def test_use_skill_opens_a_skill_mid_turn(tmp_path):
    llm = Script(ToolCall(USE_SKILL_NAME, {"name": "research-papers"}, "s1"))
    asyncio.run(_agent(tmp_path, llm).handle("hmm", TurnCallbacks(say=_say, confirm=_yes)))
    names, _ = llm.seen[1]
    assert "research__community_pulse" in names           # the skill's server came with it


def test_source_ratings_and_paper_ids():
    assert research.credibility("https://arxiv.org/abs/2304.03442")[0] > research.credibility("https://x.blogspot.com/a")[0]
    assert research.paper_id("https://huggingface.co/papers/2602.08025v2") == "2602.08025v2"
    with pytest.raises(ValueError):
        research.paper_id("no id here")


def test_failed_source_is_reported_as_failed_not_quiet(monkeypatch):
    def boom(*a):
        raise RuntimeError("403")
    monkeypatch.setitem(research.SOURCES, "reddit", boom)
    monkeypatch.setitem(research.SOURCES, "github", lambda *a: [])
    out = research.community_pulse("anything", sources="reddit,github")
    assert "reddit failed" in out and "github no results" in out
