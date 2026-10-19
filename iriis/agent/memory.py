"""Short-term conversation history, trimmed to a token budget."""
from __future__ import annotations

from typing import Any


def estimate_tokens(msg: dict[str, Any]) -> int:
    text = str(msg.get("content", "")) + str(msg.get("tool_calls", ""))
    return len(text) // 4 + 4


class ConversationHistory:
    def __init__(self, max_tokens: int = 5000):
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = []
        self.dropped: list[dict[str, Any]] = []   # trimmed-off messages, waiting to be summarised

    def add(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)
        self.trim()

    def extend(self, msgs: list[dict[str, Any]]) -> None:
        self.messages.extend(msgs)
        self.trim()

    def reset(self) -> None:
        self.messages.clear()
        self.dropped.clear()

    def trim(self) -> None:
        """Drop the oldest whole turns (a user message plus everything up to the next one)."""
        while sum(estimate_tokens(m) for m in self.messages) > self.max_tokens and len(self.messages) > 1:
            # find start of the second user turn; drop everything before it
            idx = next((i for i, m in enumerate(self.messages[1:], 1) if m["role"] == "user"), None)
            if idx is None:
                # a single huge turn: truncate oversized tool outputs instead
                for m in self.messages:
                    # (the marker makes it longer than 2000 again: never re-truncate the same message)
                    if m["role"] == "tool" and len(m["content"]) > 2000 and not m["content"].endswith("[truncated]"):
                        m["content"] = m["content"][:2000] + "\n[truncated]"
                        break
                else:
                    break
                continue
            self.dropped.extend(self.messages[:idx])
            del self.messages[:idx]
