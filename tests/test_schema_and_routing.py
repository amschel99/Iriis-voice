import asyncio

from iriis.agent.tool_router import ToolRouter
from iriis.llm.sentences import SentenceChunker
from iriis.mcp_client.schema_convert import mcp_tool_to_llm, split_name


def test_mcp_tool_to_llm_namespaces_and_cleans_schema():
    tool = {
        "name": "read_text_file",
        "description": "Read a file",
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "title": "args",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
                "opts": {"$ref": "#/$defs/Opts"},
            },
            "required": ["path"],
            "additionalProperties": False,
            "$defs": {"Opts": {"type": "object", "properties": {"bin": {"type": "boolean"}}}},
        },
    }
    out = mcp_tool_to_llm("files", tool)
    fn = out["function"]
    assert out["type"] == "function"
    assert fn["name"] == "files__read_text_file"
    params = fn["parameters"]
    assert params["required"] == ["path"]
    assert "$schema" not in params and "additionalProperties" not in params and "$defs" not in params
    assert params["properties"]["limit"] == {"type": "integer"}
    assert params["properties"]["opts"]["properties"]["bin"]["type"] == "boolean"
    assert split_name(fn["name"]) == ("files", "read_text_file")


def test_mcp_tool_object_with_attributes():
    class T:
        name = "now"
        description = None
        input_schema = None
    fn = mcp_tool_to_llm("time", T())["function"]
    assert fn["parameters"] == {"type": "object", "properties": {}}
    assert fn["description"] == "now"


def _tool(server, name, desc=""):
    return {"type": "function", "function": {"name": f"{server}__{name}", "description": desc, "parameters": {}}}


def _catalog():
    return {
        "time_and_calc": {"description": "time and maths", "keywords": ["time"], "tools": [_tool("time_and_calc", f"t{i}") for i in range(5)]},
        "system_info": {"description": "cpu ram disk battery", "keywords": ["battery"], "tools": [_tool("system_info", f"s{i}") for i in range(7)]},
        "files": {"description": "search files folders", "keywords": ["file", "document"], "tools": [_tool("files", f"f{i}") for i in range(9)]},
        "apps": {"description": "open apps volume music", "keywords": ["volume", "music"], "tools": [_tool("apps", f"a{i}") for i in range(7)]},
    }


def test_router_passes_everything_when_small():
    router = ToolRouter(max_tools=100)
    tools = asyncio.run(router.select("anything", _catalog()))
    assert len(tools) == 28


def test_router_selects_relevant_servers():
    router = ToolRouter(max_tools=20)
    tools = asyncio.run(router.select("how much battery do I have left", _catalog()))
    servers = {t["function"]["name"].split("__")[0] for t in tools}
    assert "system_info" in servers and "files" not in servers and "apps" not in servers
    assert "time_and_calc" in servers  # core server always present
    assert len(tools) <= 20

    tools = asyncio.run(router.select("turn the volume up", _catalog()))
    assert {t["function"]["name"].split("__")[0] for t in tools} >= {"apps"}


def test_router_llm_method_falls_back_to_keywords_on_error():
    async def broken(q, cat):
        raise RuntimeError("no llm")
    router = ToolRouter(max_tools=20, method="llm")
    tools = asyncio.run(router.select("find my document", _catalog(), broken))
    assert any(t["function"]["name"].startswith("files__") for t in tools)


def test_sentence_chunker_streams_sentences():
    c = SentenceChunker()
    out = []
    for tok in ["Hello the", "re. It is ", "3.5 degrees in Nai", "robi today! Dr. Otieno said", " hi. And"]:
        out += c.push(tok)
    out.append(c.flush())
    assert out == ["Hello there.", "It is 3.5 degrees in Nairobi today!", "Dr. Otieno said hi.", "And"]


def test_sentence_chunker_early_first_chunk():
    c = SentenceChunker(first_chunk_chars=40)
    out = c.push("Well, the weather in Nairobi this afternoon is looking warm, with a little cloud")
    assert out and out[0].endswith(",")


def test_web_search_is_always_offered_even_without_keywords():
    cat = _catalog()
    cat["web"] = {"description": "search", "keywords": [], "tools": [_tool("web", "web_search"), _tool("web", "read_page")]}
    router = ToolRouter(max_tools=20)
    tools = asyncio.run(router.select("did arsenal win yesterday", cat))       # no web keyword in the question
    assert any(t["function"]["name"] == "web__web_search" for t in tools)


def test_she_is_told_honestly_when_the_pc_link_is_down(tmp_path):
    from iriis.agent.loop import Agent
    from iriis.agent.permissions import PermissionManager
    from iriis.config import Config

    class CloudOnly:
        def servers_catalog(self):
            return {"time_and_calc": {"description": "", "keywords": [], "tools": [_tool("time_and_calc", "now")]}}

    cfg = Config()
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    agent = Agent(cfg, None, CloudOnly(), PermissionManager(cfg, []))
    asyncio.run(agent._select_tools("search the news"))
    system = agent._system()["content"]
    assert "computer is down" in system or "link to the user's computer is down" in system
    assert "never say you have no internet access" in system


def test_whatsapp_requests_get_the_browser_with_the_real_catalog():
    """160 tools, 64 per turn: 'who messaged me on whatsapp' must still reach the browser (it used not to)."""
    import asyncio

    from iriis.agent.tool_router import ToolRouter
    from iriis.config import load_mcp_servers

    def fake(server, n):
        return [{"type": "function", "function": {"name": f"{server}__t{i}", "description": f"{server} tool {i}",
                                                  "parameters": {}}} for i in range(n)]
    sizes = {"seryn": 60, "browser": 22, "files": 9, "google": 9, "money": 9, "studio": 5, "documents": 7,
             "shell": 1, "web": 3, "tasks": 5, "self": 5, "time_and_calc": 8, "memory": 5, "notes_reminders": 8}
    cat = {s.name: {"description": s.description, "keywords": s.keywords, "tools": fake(s.name, sizes.get(s.name, 4))}
           for s in load_mcp_servers() if s.enabled}
    for q in ("check who messaged me lately on whatsapp", "any new whatsapp chats?", "reply to mum on whatsapp"):
        picked = asyncio.run(ToolRouter(64).select(q, cat))
        assert any(t["function"]["name"].startswith("browser__") for t in picked), q
        assert len(picked) <= 64
