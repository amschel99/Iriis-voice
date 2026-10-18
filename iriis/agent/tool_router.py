"""Pick a relevant subset of tools for a turn, because small models degrade with many tools."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

# Always available regardless of routing: cheap and broadly useful.
# Always offered: cheap (few tools) and broadly useful. Web search is here so she never thinks she's offline.
CORE_SERVERS = ("time_and_calc", "memory", "web")

_STOP = set("a an the to of in on for and or is are be me my i you it this that what how can please could would with do does at".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


def _stem(w: str) -> str:
    for suf in ("ing", "es", "ed", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


class ToolRouter:
    def __init__(self, max_tools: int = 20, method: str = "keyword"):
        self.max_tools = max_tools
        self.method = method

    def _server_scores(self, query: str, servers: dict[str, dict[str, Any]]) -> dict[str, float]:
        q = {_stem(w) for w in _words(query)}
        scores: dict[str, float] = {}
        for name, info in servers.items():
            vocab = {_stem(w) for w in _words(" ".join([name.replace("_", " "), info.get("description", ""), " ".join(info.get("keywords", []))]))}
            tool_vocab = set()
            for t in info["tools"]:
                fn = t["function"]
                tool_vocab |= {_stem(w) for w in _words(fn["name"].replace("_", " ") + " " + fn.get("description", ""))}
            scores[name] = 3 * len(q & vocab) + len(q & tool_vocab)
        return scores

    async def select(
        self,
        query: str,
        servers: dict[str, dict[str, Any]],
        llm_pick: Callable[[str, dict[str, str]], Awaitable[list[str]]] | None = None,
    ) -> list[dict[str, Any]]:
        """servers: name -> {"description", "keywords", "tools": [llm tool dicts]}."""
        all_tools = [t for s in servers.values() for t in s["tools"]]
        if len(all_tools) <= self.max_tools:
            return all_tools

        chosen: list[str] = []
        if self.method == "llm" and llm_pick is not None:
            try:
                catalog = {n: s.get("description", "") for n, s in servers.items()}
                chosen = [n for n in await llm_pick(query, catalog) if n in servers]
            except Exception:
                chosen = []
        if not chosen:
            scores = self._server_scores(query, servers)
            chosen = [n for n, sc in sorted(scores.items(), key=lambda kv: -kv[1]) if sc > 0]

        selected: list[dict[str, Any]] = []
        for name in [*[c for c in CORE_SERVERS if c in servers], *chosen]:
            for t in servers[name]["tools"]:
                if t not in selected and len(selected) < self.max_tools:
                    selected.append(t)
        return selected
