"""Skills: packaged know-how Iriis loads when a request needs it (the agent-skills SKILL.md format).

A skill is a folder under skills/ with a SKILL.md: YAML front matter (name, description, keywords, servers) and a
body of instructions. Every turn she sees a one-line index of her skills; the best-matching skill for the request is
loaded in full into her system prompt, and the MCP servers it names are added to the turn's tools. She can also open
any skill herself with the use_skill tool. Skills are instructions she follows, so only skills we wrote or reviewed
belong in the folder (third-party skills are read, adapted and rewritten, never dropped in as-is).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from iriis.agent.tool_router import _stem, _words

log = logging.getLogger("iriis.skills")
SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
MAX_LOADED_CHARS = 7000      # full skill text added to one turn's prompt (the best match; a second if both are short)
MIN_SCORE = 4                # a keyword phrase hit is worth 3, a word shared with the description 1

USE_SKILL_NAME = "use_skill"


@dataclass
class Skill:
    name: str
    description: str
    body: str
    keywords: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    path: Path | None = None

    def score(self, query: str) -> int:
        q = " " + re.sub(r"[^a-z0-9]+", " ", query.lower()) + " "
        hits = sum(3 for k in self.keywords if " " + k.lower().strip() + " " in q)
        words = {_stem(w) for w in _words(query)}
        return hits + len(words & {_stem(w) for w in _words(self.description)})


def parse(text: str, path: Path | None = None) -> Skill:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        raise ValueError("SKILL.md needs YAML front matter between --- lines")
    meta = yaml.safe_load(m.group(1)) or {}
    if not meta.get("name") or not meta.get("description"):
        raise ValueError("SKILL.md front matter needs name and description")
    return Skill(name=str(meta["name"]), description=str(meta["description"]).strip(), body=m.group(2).strip(),
                 keywords=[str(k) for k in meta.get("keywords") or []], servers=[str(s) for s in meta.get("servers") or []],
                 path=path)


class SkillLibrary:
    def __init__(self, folder: Path = SKILLS_DIR):
        self.folder = folder
        self.skills: dict[str, Skill] = {}
        self.load()

    def load(self) -> list[str]:
        problems, found = [], {}
        for f in sorted(self.folder.glob("*/SKILL.md")) if self.folder.exists() else []:
            try:
                s = parse(f.read_text(encoding="utf-8"), f)
                found[s.name] = s
            except Exception as e:  # noqa: BLE001
                problems.append(f"{f.parent.name}: {e}")
                log.warning("skill %s not loaded: %s", f.parent.name, e)
        self.skills = found
        return problems

    def get(self, name: str) -> Skill | None:
        key = name.strip().lower().replace(" ", "-")
        return self.skills.get(key) or next((s for s in self.skills.values() if key in s.name), None)

    def match(self, query: str, limit: int = 2) -> list[Skill]:
        ranked = sorted(((s.score(query), s) for s in self.skills.values()), key=lambda x: -x[0])
        picked = [s for sc, s in ranked if sc >= MIN_SCORE][:limit]
        # a second skill only when both fit comfortably
        if len(picked) == 2 and len(picked[0].body) + len(picked[1].body) > MAX_LOADED_CHARS:
            picked = picked[:1]
        return picked

    def index(self) -> str:
        if not self.skills:
            return ""
        rows = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())
        return ("Your skills (step-by-step know-how; when one fits the job and isn't loaded below, open it with "
                f"use_skill before you start):\n{rows}")

    def prompt(self, loaded: list[Skill]) -> str:
        parts = [self.index()]
        for s in loaded:
            parts.append(f"Skill loaded for this request: {s.name}. Follow it.\n<skill name=\"{s.name}\">\n"
                         f"{s.body[:MAX_LOADED_CHARS]}\n</skill>")
        return "\n\n".join(p for p in parts if p)

    def tool(self) -> dict:
        return {"type": "function", "function": {
            "name": USE_SKILL_NAME,
            "description": "Open one of your skills (its full instructions, and the tools it needs). Use when a job "
                           "matches a skill in your list that isn't loaded yet.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "enum": sorted(self.skills)}}, "required": ["name"]}}}


_LIBRARY: SkillLibrary | None = None


def library() -> SkillLibrary:
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = SkillLibrary()
    return _LIBRARY
