import json

from iriis.agent.permissions import AuditLog, PermissionManager, describe_call, is_affirmative, redact_secrets
from iriis.config import Config, MCPServerConfig


def make_pm(**perm):
    cfg = Config()
    for k, v in perm.items():
        setattr(cfg.permissions, k, v)
    servers = [
        MCPServerConfig(name="files", tier="auto", tools={"write_text_file": "confirm", "delete_*": "confirm"}),
        MCPServerConfig(name="shell", tier="confirm"),
        MCPServerConfig(name="danger", tier="blocked"),
    ]
    return PermissionManager(cfg, servers)


def test_server_tier_and_tool_overrides():
    pm = make_pm()
    assert pm.decide("files__read_text_file", {}).tier == "auto"
    assert pm.decide("files__write_text_file", {}).tier == "confirm"
    assert pm.decide("files__delete_file", {}).tier == "confirm"  # glob
    assert pm.decide("danger__anything", {}).tier == "blocked"


def test_unknown_tools_default_to_confirm():
    pm = make_pm()
    assert pm.decide("newserver__do_stuff", {}).tier == "confirm"
    assert pm.decide("weird", {}).tier == "confirm"


def test_global_overrides_win():
    pm = make_pm(overrides={"files__delete_file": "blocked", "files__*": "confirm"})
    assert pm.decide("files__delete_file", {}).tier == "blocked"
    assert pm.decide("files__read_text_file", {}).tier == "confirm"


def test_always_blocked_names():
    pm = make_pm(overrides={"vault__read_password": "auto"})
    assert pm.decide("vault__read_password", {}).tier == "blocked"
    assert pm.decide("disk__format_drive", {}).tier == "blocked"


def test_shell_classifier_is_argument_aware():
    pm = make_pm()
    assert pm.decide("shell__run_command", {"command": "git status"}).tier == "auto"
    assert pm.decide("shell__run_command", {"command": "git push"}).tier == "confirm"
    assert pm.decide("shell__run_command", {"command": "rm -rf /"}).tier == "blocked"


def test_blocked_shell_server_stays_blocked_even_for_safe_commands():
    pm = make_pm(overrides={"shell__run_command": "blocked"})
    assert pm.decide("shell__run_command", {"command": "ls"}).tier == "blocked"


def test_affirmative_detection_is_strict():
    words = Config().permissions.confirm_words
    for yes in ["yes", "Yes please.", "yeah go ahead", "confirm", "OK", "sure, do it"]:
        assert is_affirmative(yes, words), yes
    for no in ["no", "no don't", "wait", "yes no", "not yet", "cancel", "", "what?", "hmm maybe", "never mind"]:
        assert not is_affirmative(no, words), no


def test_redaction():
    text = "API_KEY=sk-abcdefghijklmnopqrstuvwx\npassword: hunter2\ntoken ghp_abcdefghijklmnopqrstuvwxyz123456\nok line"
    out = redact_secrets(text)
    assert "hunter2" not in out and "sk-abc" not in out and "ghp_" not in out
    assert "ok line" in out


def test_audit_log_writes_jsonl(tmp_path):
    log = AuditLog(str(tmp_path / "a.jsonl"))
    log.record(tool="x__y", args={"password": "p", "q": 1}, tier="auto", confirmed=None, executed=True, result="done")
    entry = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert entry["tool"] == "x__y" and entry["executed"] is True and entry["tier"] == "auto"


def test_describe_call():
    s = describe_call("files__move_file", {"source": "a.txt", "destination": "b.txt"})
    assert "move file" in s and "a.txt" in s and s.endswith("Shall I go ahead?")


def test_page_scripts_are_asked_in_plain_words():
    from iriis.agent.permissions import describe_call
    q = describe_call("browser__browser_evaluate", {"function": "() => { const rows = document.querySelectorAll('x') }"})
    assert "querySelector" not in q and "script" in q and "10 minutes" in q


def test_one_yes_covers_repeat_page_scripts(tmp_path):
    import asyncio

    from iriis.agent.loop import Agent, TurnCallbacks
    from iriis.config import Config, MCPServerConfig
    from iriis.llm.base import ToolCall

    class Tools:
        def servers_catalog(self):
            return {}

        async def call_tool(self, name, args, confirmed=False):
            return "ok"

    cfg = Config()
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    pm = PermissionManager(cfg, [MCPServerConfig(name="browser", tier="auto", tools={"browser_evaluate": "confirm"})])
    agent = Agent(cfg, None, Tools(), pm)
    asked = []

    async def confirm(q):
        asked.append(q)
        return "yes"

    async def say(_):
        pass
    cb = TurnCallbacks(say=say, confirm=confirm)
    known = {"browser__browser_evaluate"}
    for _ in range(3):
        assert asyncio.run(agent._execute(ToolCall("browser__browser_evaluate", {"function": "() => 1"}, "x"), known, cb)) == "ok"
    assert len(asked) == 1


def test_commands_are_asked_about_by_purpose_not_code():
    from iriis.agent.permissions import describe_call
    q = describe_call("shell__run_command", {"command": "$src=\"$env:LOCALAPPDATA\Brave\"; Copy-Item ...", "purpose": "List the open Brave tabs"})
    assert "list the open Brave tabs" in q and "$env" not in q and "yes to all" in q


def test_yes_to_all_stops_repeat_questions(tmp_path):
    import asyncio

    from iriis.agent.loop import Agent, TurnCallbacks
    from iriis.config import Config, MCPServerConfig
    from iriis.llm.base import ToolCall

    class Tools:
        def servers_catalog(self):
            return {}

        async def call_tool(self, name, args, confirmed=False):
            return "done"

    cfg = Config()
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    agent = Agent(cfg, None, Tools(), PermissionManager(cfg, [MCPServerConfig(name="shell", tier="confirm")]))
    asked = []

    async def confirm(q):
        asked.append(q)
        return "yes to all"

    async def say(_):
        pass
    cb = TurnCallbacks(say=say, confirm=confirm)
    for cmd in ("Copy-Item a b", "Remove-Item b"):
        call = ToolCall("shell__run_command", {"command": cmd, "purpose": "tidy up"}, "x")
        assert asyncio.run(agent._execute(call, {"shell__run_command"}, cb)) == "done"
    assert len(asked) == 1
