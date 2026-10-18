import asyncio
import json

import httpx

from iriis.config import LLMConfig, load_config
from iriis.llm.openai_client import OpenAICompatClient


def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def make(monkeypatch, handler, **cfg):
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    client = OpenAICompatClient(LLMConfig(backend="openai", **cfg))
    client.http = httpx.AsyncClient(base_url="https://api.groq.com/openai/v1", transport=httpx.MockTransport(handler),
                                    headers=client.http.headers)
    return client


async def collect(client, **kw):
    return [ev async for ev in client.chat_stream([{"role": "user", "content": "hi"}], **kw)]


def test_streams_text_and_fragmented_tool_calls(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        body = sse(
            {"choices": [{"delta": {"content": "Checking "}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "time_and_calc__calc", "arguments": "{\"expr"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "ulate", "arguments": "ession\": \"2+2\"}"}}]}}]},
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = make(monkeypatch, handler)
    events = asyncio.run(collect(client, tools=[{"type": "function", "function": {"name": "x"}}]))
    assert "".join(e.text for e in events) == "Checking "
    call = events[-1].tool_calls[0]
    assert call.name == "time_and_calc__calculate" and call.arguments == {"expression": "2+2"} and call.id == "call_1"
    assert seen["auth"] == "Bearer gk"
    assert seen["body"]["model"] == "openai/gpt-oss-120b" and seen["body"]["reasoning_effort"] == "low"
    assert seen["body"]["tool_choice"] == "auto"


def test_rate_limit_falls_back_to_smaller_model(monkeypatch):
    models = []

    def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        if body["model"] == "openai/gpt-oss-120b":
            return httpx.Response(429, text='{"error": "rate limited"}')
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}))

    client = make(monkeypatch, handler)
    events = asyncio.run(collect(client))
    assert models == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert "".join(e.text for e in events) == "ok"


def test_tool_history_is_converted_to_openai_shape():
    msgs = OpenAICompatClient._to_openai([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "a__b", "arguments": {"x": 1}}}]},
        {"role": "tool", "content": "res", "tool_name": "a__b", "tool_call_id": "c1"},
    ])
    assert msgs[0]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}' and msgs[0]["content"] is None
    assert msgs[1] == {"role": "tool", "content": "res", "tool_call_id": "c1"}


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("IRIIS__LLM__MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("IRIIS__SERVER__CLOUD_SERVERS", "[time_and_calc]")
    monkeypatch.setenv("IRIIS__SERVER__STT", "false")
    cfg = load_config()
    assert cfg.llm.model == "openai/gpt-oss-20b"
    assert cfg.server.cloud_servers == ["time_and_calc"] and cfg.server.stt is False
