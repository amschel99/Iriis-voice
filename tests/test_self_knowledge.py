"""Iriis reads her own setup and code (never secrets)."""
from __future__ import annotations

import pytest

from servers import self_knowledge as S


def test_about_me_lists_skills_and_where_they_run():
    out = S.about_me()
    assert "telegram" not in out.lower() or "Telegram" in out
    for name in ("web:", "tasks:", "browser:", "seryn:", "money:"):
        assert name in out
    assert "in the cloud" in out and "on the user's computer" in out and "Always asks first for" in out


def test_code_search_and_read():
    assert "iriis/server/telegram.py" in S.search_my_code("telegram chat id")
    assert "lines 1-" in S.read_my_code("iriis/agent/tasks.py", 1, 20)
    assert "servers/web.py" in S.list_my_files("servers")


@pytest.mark.parametrize("bad", [".env", "../.env", "data/iriis.sqlite3", "C:/Windows/win.ini", "../seryn-twin/README.md"])
def test_never_reads_secrets_or_outside(bad):
    with pytest.raises(ValueError):
        S.read_my_code(bad)


def test_self_is_always_offered():
    from iriis.agent.tool_router import CORE_SERVERS
    assert "self" in CORE_SERVERS
