"""Camera mode as one system: the camera model answers fast, then her main brain does whatever else is needed."""
from __future__ import annotations

import asyncio
import json

from tests.test_server import make_client


class FakeVision:
    """Stands in for Seryn Vision's /ws: answers every ask at once."""

    def __init__(self):
        self.sent: list = []
        self.q: asyncio.Queue = asyncio.Queue()

    async def send(self, m):
        self.sent.append(m)
        if isinstance(m, str) and json.loads(m).get("type") == "ask":
            await self.q.put(json.dumps({"type": "answer", "say": "A fridge and two cold packs on a table.", "status": "ok"}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.q.get()

    async def close(self):
        pass


def run_call(monkeypatch, tmp_path, script, question):
    monkeypatch.setenv("VISION_MCP_URL", "https://bench.example/mcp")
    monkeypatch.setenv("VISION_ACCESS_KEY", "vk")
    vision = FakeVision()

    async def connect(*a, **k):
        return vision
    import websockets
    monkeypatch.setattr(websockets, "connect", connect)
    client, llm = make_client(monkeypatch, script, tmp_path)
    got = []
    with client:
        with client.websocket_connect("/v1/camera?token=test-key") as ws:
            ws.send_text(json.dumps({"type": "ask", "text": question}))
            for _ in range(6):
                m = json.loads(ws.receive_text())
                got.append(m)
                if m["type"] in ("brain", "brain_done"):
                    break
        history = client.get("/v1/history", headers={"Authorization": "Bearer test-key"}).json()
    return got, history, llm


def test_plain_question_stays_with_the_fast_eyes(monkeypatch, tmp_path):
    got, history, _ = run_call(monkeypatch, tmp_path, ["SILENT"], "what do you see?")
    assert [m["type"] for m in got] == ["answer", "brain_done"]
    assert [h["content"] for h in history][-2:] == ["[camera] what do you see?", "A fridge and two cold packs on a table."]


def test_brain_follows_up_when_more_is_wanted(monkeypatch, tmp_path):
    got, history, llm = run_call(monkeypatch, tmp_path, ["I've written it up for you."],
                                 "summarise what you see in a pdf for me")
    assert got[-1] == {"type": "brain", "say": "I've written it up for you.", "files": []}
    prompt = llm.seen[0][-1]["content"]
    assert "summarise what you see in a pdf" in prompt and "A fridge and two cold packs" in prompt
    assert history[-1]["content"].startswith("I've written it up for you.")
