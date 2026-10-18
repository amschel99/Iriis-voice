"""Cloud core API tests with a scripted fake LLM (no network, no models)."""
import json
import os
import threading
import time

import pytest

os.environ["IRIIS_NO_DEFAULT_APP"] = "1"
os.environ["IRIIS_API_KEY"] = "test-key"
os.environ["IRIIS_DESKTOP_TOKEN"] = "desk-token"

from fastapi.testclient import TestClient  # noqa: E402

from iriis.config import Config  # noqa: E402
from iriis.llm.base import LLMClient, LLMEvent, ToolCall  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}


class FakeLLM(LLMClient):
    """Plays a script: each entry is either a list of ToolCalls or a reply string."""

    def __init__(self, script):
        self.model = "fake"
        self.script = list(script)
        self.seen: list[list[dict]] = []

    async def chat_stream(self, messages, tools=None, model=None):
        self.seen.append(messages)
        step = self.script.pop(0) if self.script else "Done."
        if isinstance(step, list):
            yield LLMEvent(tool_calls=step, done=True)
            return
        for word in step.split(" "):
            yield LLMEvent(text=word + " ")
        yield LLMEvent(done=True)

    async def health(self):
        return True, "fake ok"


def make_client(monkeypatch, script, tmp_path):
    import iriis.app as japp
    from iriis.server.app import create_app

    fake = FakeLLM(script)
    monkeypatch.setattr(japp, "make_llm", lambda cfg: fake)
    cfg = Config()
    cfg.server.cloud_servers = ["time_and_calc"]
    cfg.server.stt = False
    cfg.server.tts = False
    monkeypatch.setenv("IRIIS_SQLITE_PATH", str(tmp_path / "iriis.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg.permissions.audit_log = str(tmp_path / "audit.jsonl")
    return TestClient(create_app(cfg)), fake


def sse_events(resp):
    events = []
    for block in resp.text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_health_and_llms_txt_are_public(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        assert client.get("/health").json() == {"ok": True}
        txt = client.get("/llms.txt").text
        assert "/v1/chat" in txt and "/mcp" in txt and "{BASE_URL}" not in txt


def test_auth_required(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, ["hi"], tmp_path)
    with client:
        assert client.post("/v1/chat", json={"message": "hi", "stream": False}).status_code == 401
        assert client.post("/v1/chat", json={"message": "hi", "stream": False},
                           headers={"Authorization": "Bearer nope"}).status_code == 401
        r = client.post("/v1/chat", json={"message": "hi", "stream": False}, headers=AUTH)
        assert r.status_code == 200 and r.json()["text"].strip() == "hi"


def test_sse_chat_runs_cloud_tool(monkeypatch, tmp_path):
    script = [[ToolCall("time_and_calc__calculate", {"expression": "15% of 2400"}, "c1")], "That is 360 shillings."]
    client, fake = make_client(monkeypatch, script, tmp_path)
    with client:
        r = client.post("/v1/chat", json={"message": "what's 15% of 2400", "stream": True}, headers=AUTH)
        events = sse_events(r)
    kinds = [k for k, _ in events]
    assert kinds[0] == "session" and kinds[-1] == "done"
    assert any(k == "sentence" and "360" in e["text"] for k, e in events)
    # the real MCP tool ran and its (untrusted-wrapped) result reached the LLM
    tool_msgs = [m for m in fake.seen[-1] if m["role"] == "tool"]
    assert "= 360" in tool_msgs[0]["content"] and "untrusted" in tool_msgs[0]["content"]
    done = events[-1][1]
    assert done["tool_calls"][0]["name"] == "time_and_calc__calculate"


def test_session_memory_persists(monkeypatch, tmp_path):
    client, fake = make_client(monkeypatch, ["first.", "second."], tmp_path)
    with client:
        sid = client.post("/v1/chat", json={"message": "one", "stream": False}, headers=AUTH).json()["session_id"]
        client.post("/v1/chat", json={"message": "two", "session_id": sid, "stream": False}, headers=AUTH)
    users = [m["content"] for m in fake.seen[-1] if m["role"] == "user"]
    assert users == ["one", "two"]


DESKTOP_HELLO = {
    "type": "hello",
    "confirm_mode": "cloud",
    "servers": [{"name": "files", "tier": "auto", "tools": {"write_text_file": "confirm"}}],
    "catalog": {"files": {"description": "files", "keywords": ["file"], "tools": [
        {"type": "function", "function": {"name": "files__read_text_file", "description": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "files__write_text_file", "description": "write", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    ]}},
}


def test_desktop_requires_its_own_token(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        with pytest.raises(Exception):
            with client.websocket_connect("/v1/desktop?token=test-key") as ws:  # API key is not enough
                ws.send_json(DESKTOP_HELLO)
                ws.receive_json()


def _run_desktop(client, replies, received):
    """Pretend to be the desktop agent: answer every call with the next reply."""
    def run():
        with client.websocket_connect("/v1/desktop?token=desk-token") as ws:
            ws.send_json(DESKTOP_HELLO)
            for reply in replies:
                msg = ws.receive_json()
                received.append(msg)
                ws.send_json({"type": "result", "id": msg["id"], "content": reply})
            time.sleep(0.5)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.5)
    return t


def test_desktop_tool_call_round_trip(monkeypatch, tmp_path):
    script = [[ToolCall("files__read_text_file", {"path": "notes.txt"}, "c1")], "Your note says buy milk."]
    client, fake = make_client(monkeypatch, script, tmp_path)
    received = []
    with client:
        t = _run_desktop(client, ["buy milk"], received)
        status = client.get("/v1/status", headers=AUTH).json()
        assert status["desktop_connected"] is True
        r = client.post("/v1/chat", json={"message": "read my note", "stream": False}, headers=AUTH)
        t.join(5)
    assert received[0]["name"] == "files__read_text_file" and received[0]["confirmed"] is False
    assert "buy milk" in r.json()["text"]


def test_confirmation_flow_over_sse(monkeypatch, tmp_path):
    script = [[ToolCall("files__write_text_file", {"path": "a.txt"}, "c1")], "Saved it."]
    client, fake = make_client(monkeypatch, script, tmp_path)
    received = []
    with client:
        t = _run_desktop(client, ["Wrote a.txt"], received)
        result = {}

        def chat():
            r = client.post("/v1/chat", json={"message": "save a file", "stream": True}, headers=AUTH)
            result["events"] = sse_events(r)

        ct = threading.Thread(target=chat)
        ct.start()
        # wait for the pending confirmation, then approve it
        deadline = time.time() + 10
        sess = None
        while time.time() < deadline:
            sessions = client.app.state.core.sessions.sessions
            sess = next((s for s in sessions.values() if s.pending), None)
            if sess:
                break
            time.sleep(0.05)
        assert sess is not None, "no confirmation was requested"
        assert client.post("/v1/confirm", json={"session_id": sess.id, "approved": True}, headers=AUTH).json() == {"ok": True}
        ct.join(10)
        t.join(5)
    kinds = [k for k, _ in result["events"]]
    assert "confirm" in kinds
    assert received and received[0]["confirmed"] is True


def test_declined_confirmation_never_reaches_desktop(monkeypatch, tmp_path):
    script = [[ToolCall("files__write_text_file", {"path": "a.txt"}, "c1")], "Okay, I won't."]
    client, fake = make_client(monkeypatch, script, tmp_path)
    received = []
    with client:
        t = _run_desktop(client, [], received)
        client.app.state.core.cfg.server.confirm_timeout_s = 0.5   # nobody answers -> declined
        r = client.post("/v1/chat", json={"message": "save a file", "stream": False}, headers=AUTH)
        t.join(3)
    assert received == []
    tool_msgs = [m for m in fake.seen[-1] if m["role"] == "tool"]
    assert "DECLINED" in tool_msgs[0]["content"]


def test_owner_conversation_survives_server_restart(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, ["Noted, Thursday it is."], tmp_path)
    with client:
        client.post("/v1/chat", json={"message": "the plumber comes on Thursday", "session_id": "owner", "stream": False}, headers=AUTH)
    # A brand-new server process (same database) must still have the context.
    client2, fake2 = make_client(monkeypatch, ["Thursday."], tmp_path)
    with client2:
        r = client2.post("/v1/chat", json={"message": "when is the plumber coming?", "session_id": "owner", "stream": False}, headers=AUTH)
        # ...and ?purge=true really forgets it
        assert client2.delete("/v1/sessions/owner?purge=true", headers=AUTH).json() == {"ok": True, "purged": True}
        assert client2.app.state.core.conversations.search("plumber") == []
    users = [m["content"] for m in fake2.seen[-1] if m["role"] == "user"]
    assert users == ["the plumber comes on Thursday", "when is the plumber coming?"]
    assert r.json()["session_id"] == "owner"


def test_new_desktop_connection_replaces_a_stale_one(monkeypatch, tmp_path):
    script = [[ToolCall("files__read_text_file", {"path": "a"}, "c1")], "Done."]
    client, _ = make_client(monkeypatch, script, tmp_path)
    with client:
        with client.websocket_connect("/v1/desktop?token=desk-token") as stale:
            stale.send_json(DESKTOP_HELLO)
            time.sleep(0.3)
            received = []
            t = _run_desktop(client, ["from the new agent"], received)   # connects while `stale` is still open
            r = client.post("/v1/chat", json={"message": "read a", "stream": False}, headers=AUTH)
            t.join(5)
    assert received and received[0]["name"] == "files__read_text_file"   # the NEW agent got the call
    assert r.status_code == 200
