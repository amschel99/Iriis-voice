"""Ollama backend, talking to the local HTTP API (http://127.0.0.1:11434/api/chat)."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from iriis.config import LLMConfig
from iriis.llm.base import LLMClient, LLMEvent, ToolCall

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>\s*")


def _assert_local(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"LLM host must be local (got {host!r}); Iriis never sends prompts off this machine.")


class OllamaClient(LLMClient):
    def __init__(self, cfg: LLMConfig):
        _assert_local(cfg.host)
        self.cfg = cfg
        self.model = cfg.model
        self.http = httpx.AsyncClient(base_url=cfg.host, timeout=httpx.Timeout(cfg.request_timeout_s, connect=5.0), trust_env=False)

    def _options(self) -> dict[str, Any]:
        return {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx}

    @staticmethod
    def _to_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            m = {k: v for k, v in m.items() if k not in ("tool_call_id", "reasoning_content")}
            if m.get("tool_calls"):
                m["tool_calls"] = [{"function": tc["function"]} for tc in m["tool_calls"]]
            out.append(m)
        return out

    async def chat_stream(self, messages, tools=None, model=None) -> AsyncIterator[LLMEvent]:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": self._to_ollama(messages),
            "stream": True,
            "keep_alive": self.cfg.keep_alive,
            "options": self._options(),
            "think": self.cfg.think,
        }
        if tools:
            payload["tools"] = tools
        calls: list[ToolCall] = []
        in_think = False
        async with self.http.stream("POST", "/api/chat", json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                if "does not support thinking" in body and payload.get("think") is not None:
                    # Non-thinking models reject the flag; retry without it.
                    self.cfg.think = None  # type: ignore[assignment]
                    async for ev in self.chat_stream(messages, tools, model):
                        yield ev
                    return
                raise RuntimeError(f"Ollama error {resp.status_code}: {body[:300]}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(f"Ollama error: {chunk['error']}")
                msg = chunk.get("message") or {}
                text = msg.get("content") or ""
                # Strip inline <think> blocks from models that don't use the separate field.
                if "<think>" in text:
                    in_think = True
                if in_think:
                    if "</think>" in text:
                        text = text.split("</think>", 1)[1]
                        in_think = False
                    else:
                        text = ""
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=tc.get("id") or uuid.uuid4().hex[:8]))
                if text:
                    yield LLMEvent(text=text)
                if chunk.get("done"):
                    stats = {k: chunk.get(k) for k in ("total_duration", "load_duration", "prompt_eval_count", "eval_count", "eval_duration")}
                    yield LLMEvent(tool_calls=calls, done=True, stats=stats)
                    return

    async def health(self) -> tuple[bool, str]:
        try:
            r = await self.http.get("/api/tags")
            names = [m["name"] for m in r.json().get("models", [])]
        except Exception as e:  # noqa: BLE001
            return False, f"Ollama is not reachable at {self.cfg.host} ({e.__class__.__name__}). Is it running?"
        want = [self.cfg.model, self.cfg.fallback_model]
        missing = [m for m in want if m and not any(n == m or n == f"{m}:latest" for n in names)]
        if self.model in missing:
            return False, f"Model {self.model} is not pulled. Run: ollama pull {self.model}"
        return True, f"Ollama OK, models: {', '.join(names)}"

    async def warmup(self) -> None:
        try:
            await self.http.post("/api/chat", json={"model": self.model, "messages": [], "keep_alive": self.cfg.keep_alive})
        except Exception:
            pass

    async def aclose(self) -> None:
        await self.http.aclose()
