"""Remote MCP servers (e.g. the Seryn Twin on Railway) with auth headers from the environment."""
from __future__ import annotations

import pytest

from iriis.config import Config, MCPServerConfig, load_mcp_servers
from iriis.mcp_client.manager import MCPManager


def _cfg(**kw):
    return MCPServerConfig(name="seryn", transport="http", needs_internet=True, url="https://example.org/mcp",
                           headers={"Authorization": "Bearer ${SERYN_API_KEY}"}, **kw)


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("SERYN_API_KEY", raising=False)
    with pytest.raises(ValueError, match="SERYN_API_KEY"):
        MCPManager(Config())._target(_cfg())


def test_headers_expand_from_env(monkeypatch):
    monkeypatch.setenv("SERYN_API_KEY", "abc")
    t = MCPManager(Config())._target(_cfg())
    assert not isinstance(t, str)          # a transport carrying the header, not a bare URL


def test_seryn_runs_in_the_cloud_and_web_in_both():
    cfg = Config()
    assert {"seryn", "web"} <= set(cfg.server.cloud_servers)
    assert "seryn" not in cfg.desktop.servers and "web" in cfg.desktop.servers
    s = {x.name: x for x in load_mcp_servers()}["seryn"]
    assert s.transport == "http" and s.url.startswith("https://") and "SERYN_API_KEY" in s.headers["Authorization"]
