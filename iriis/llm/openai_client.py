"""OpenAI-compatible chat completions backend (streaming + tool calls).

Works with Groq (default, https://api.groq.com/openai/v1), OpenRouter, OpenAI, a local
llama.cpp `llama-server --jinja`, or Ollama's own /v1 endpoint.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from iriis.config import LLMConfig
from iriis.llm.base import LLMClient, LLMEvent, ToolCall

_LOCAL = ("127.0.0.1", "localhost", "::1")


class _Retryable(Exception):
    pass


class OpenAICompatClient(LLMClient):
    def __init__(self, cfg: LLMConfig, base_url: str | None = None):
        self.cfg = cfg
        self.model = cfg.model
        url = (base_url or cfg.base_url).rstrip("/")
        self.local = (urlparse(url).hostname or "") in _LOCAL
        key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
        if not key and not self.local:
            raise RuntimeError(f"Set {cfg.api_key_env} to use {url}")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = httpx.AsyncClient(base_url=url, headers=headers, trust_env=not self.local,
                                      timeout=httpx.Timeout(cfg.request_timeout_s, connect=10.0))

    @staticmethod
    def _to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            if m.get("role") == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_call_id": m.get("tool_call_id") or "call"})
                continue
            m = {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "name")}
            if m.get("tool_calls"):
                m["tool_calls"] = [
                    {"id": tc.get("id") or "call", "type": "function",
                     "function": {"name": tc["function"]["name"], "arguments": json.dumps(tc["function"]["arguments"])}}
                    for tc in m["tool_calls"]
                ]
                m["content"] = m.get("content") or None
            out.append(m)
        return out

    def _payload(self, messages, tools, model) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": self._to_openai(messages),
            "stream": True,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.cfg.reasoning_effort and "gpt-oss" in payload["model"]:
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        return payload

    async def chat_stream(self, messages, tools=None, model=None) -> AsyncIterator[LLMEvent]:
        try:
            async for ev in self._stream(messages, tools, model):
                yield ev
        except _Retryable as e:
            fallback = self.cfg.fallback_model
            if not fallback or (model or self.model) == fallback:
                raise RuntimeError(str(e)) from e
            async for ev in self._stream(messages, tools, fallback):
                yield ev

    async def _stream(self, messages, tools=None, model=None) -> AsyncIterator[LLMEvent]:
        payload = self._payload(messages, tools, model)
        partial: dict[int, dict[str, Any]] = {}
        stats: dict[str, Any] = {}
        async with self.http.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                err = f"LLM API error {resp.status_code}: {body[:400]}"
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise _Retryable(err)   # rate limit / outage: try the fallback model
                raise RuntimeError(err)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage"):
                    stats.update(chunk.get("usage") or chunk["x_groq"]["usage"])
                if not chunk.get("choices"):
                    continue
                if chunk["choices"][0].get("finish_reason"):
                    stats["finish_reason"] = chunk["choices"][0]["finish_reason"]
                delta = chunk["choices"][0].get("delta") or {}
                if delta.get("content"):
                    yield LLMEvent(text=delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = partial.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
        calls = []
        for slot in partial.values():
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": slot["args"]}
            calls.append(ToolCall(name=slot["name"], arguments=args if isinstance(args, dict) else {}, id=slot["id"] or uuid.uuid4().hex[:8]))
        yield LLMEvent(tool_calls=calls, done=True, stats=stats)

    async def health(self) -> tuple[bool, str]:
        try:
            r = await self.http.get("/models")
            if r.status_code == 401:
                return False, f"LLM API rejected the key in {self.cfg.api_key_env}"
            if r.status_code != 200:
                return (True, f"LLM API at {self.http.base_url} (no /models listing)") if self.local else (False, f"LLM API error {r.status_code}")
            ids = [m.get("id") for m in r.json().get("data", [])]
            if ids and self.model not in ids:
                return False, f"Model {self.model} not offered by {self.http.base_url}. Some available: {', '.join(ids[:8])}"
            return True, f"LLM API OK ({self.http.base_url}, {self.model})"
        except Exception as e:  # noqa: BLE001
            return False, f"LLM API not reachable at {self.http.base_url} ({e.__class__.__name__})"

    async def aclose(self) -> None:
        await self.http.aclose()


def LlamaCppClient(cfg: LLMConfig) -> OpenAICompatClient:  # noqa: N802 (kept for config compatibility)
    """llama.cpp's server speaks the same protocol under /v1 on cfg.host."""
    return OpenAICompatClient(cfg, base_url=cfg.host.rstrip("/") + "/v1")
