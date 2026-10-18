"""LLM client interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class LLMEvent:
    """One streamed event: a text delta, or (at the end) the tool calls the model wants."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    done: bool = False
    stats: dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    model: str

    @abstractmethod
    def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                    model: str | None = None) -> AsyncIterator[LLMEvent]:
        ...

    async def complete(self, messages: list[dict[str, Any]], model: str | None = None) -> str:
        out = []
        async for ev in self.chat_stream(messages, None, model=model):
            out.append(ev.text)
        return "".join(out)

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        ...

    async def warmup(self) -> None:
        """Load the model into memory so the first real request isn't a cold start."""

    async def aclose(self) -> None:
        ...

    @staticmethod
    def tool_result_message(call: ToolCall, content: str) -> dict[str, Any]:
        return {"role": "tool", "content": content, "tool_name": call.name, "tool_call_id": call.id}

    @staticmethod
    def assistant_tool_message(text: str, calls: list[ToolCall]) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": text,
            "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls],
        }
