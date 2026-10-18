"""Convert MCP tool definitions into the OpenAI/Ollama function-calling format."""
from __future__ import annotations

import copy
import re
from typing import Any

SEP = "__"
_UNSUPPORTED_KEYS = {"$schema", "additionalProperties", "$defs", "definitions", "title", "examples", "default"}


def namespaced(server: str, tool: str) -> str:
    name = f"{server}{SEP}{tool}"
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name)[:64]


def split_name(name: str) -> tuple[str, str]:
    server, _, tool = name.partition(SEP)
    return server, tool


def _resolve_refs(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 8:
        return {"type": "object"}
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"].rsplit("/", 1)[-1]
            return _resolve_refs(copy.deepcopy(defs.get(ref, {"type": "object"})), defs, depth + 1)
        return {k: _resolve_refs(v, defs, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, defs, depth + 1) for v in node]
    return node


def _simplify(node: Any) -> Any:
    """Small models choke on anyOf/nullable unions and noisy keys. Flatten what we safely can."""
    if isinstance(node, list):
        return [_simplify(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: v for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
    for key in ("anyOf", "oneOf"):
        if key in node:
            options = [o for o in node[key] if not (isinstance(o, dict) and o.get("type") == "null")]
            if len(options) == 1:
                merged = {**node, **options[0]}
                merged.pop(key, None)
                node = merged
    if "properties" in node:
        node["properties"] = {k: _simplify(v) for k, v in node["properties"].items()}
    if "items" in node:
        node["items"] = _simplify(node["items"])
    if node.get("type") == "object" and "properties" not in node:
        node["properties"] = {}
    return node


def mcp_tool_to_llm(server: str, tool: Any) -> dict[str, Any]:
    """`tool` is an mcp.types.Tool (or a dict with name/description/inputSchema)."""
    get = (lambda k: tool.get(k)) if isinstance(tool, dict) else (lambda k: getattr(tool, k, None))
    schema = copy.deepcopy(get("inputSchema") or get("input_schema") or {"type": "object", "properties": {}})
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    schema = _simplify(_resolve_refs(schema, defs))
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    description = (get("description") or "").strip()
    if len(description) > 500:
        description = description[:497] + "..."
    return {
        "type": "function",
        "function": {
            "name": namespaced(server, get("name")),
            "description": description or get("name"),
            "parameters": schema,
        },
    }
