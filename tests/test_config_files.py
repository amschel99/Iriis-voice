from iriis.config import DEFAULT_CONFIG, DEFAULT_MCP_CONFIG, load_config, load_mcp_servers


def test_shipped_config_validates():
    cfg = load_config(DEFAULT_CONFIG)
    assert "yes" in cfg.permissions.confirm_words
    assert all(isinstance(w, str) for w in cfg.permissions.confirm_words + cfg.barge_in.stop_words)
    assert cfg.llm.host.startswith("http://127.0.0.1")


def test_shipped_mcp_servers_validate():
    servers = {s.name: s for s in load_mcp_servers(DEFAULT_MCP_CONFIG)}
    builtins = {"time_and_calc", "system_info", "files", "shell", "apps_and_media", "notes_reminders", "clipboard", "memory"}
    assert builtins <= {n for n, s in servers.items() if s.enabled}
    # web search + browser are on by default; the unofficial messaging bridges always ship disabled
    assert servers["web"].enabled and servers["browser"].enabled
    for bridge in ("whatsapp", "x_twitter", "instagram", "linkedin"):
        assert not servers[bridge].enabled, bridge
    assert servers["files"].tools["delete_file"] == "confirm"
