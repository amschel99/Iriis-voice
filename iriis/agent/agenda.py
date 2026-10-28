"""Her own agenda: a daily rhythm, long-running projects she chooses, hobbies she practises, and work she does alone.

Ideas from the research on open-ended, self-directed agents:
- Planning a day from goals and reviewing it at night (Park et al. 2023, "Generative Agents").
- Proposing her own next task where she is learning most, building a library of skills (Wang et al. 2023,
  "Voyager"), choosing what is *interesting* - new relative to what she has done and valuable to the user - so she
  doesn't repeat herself (Zhang et al. 2023, "OMNI: Open-endedness via Models of human Notions of Interestingness";
  "LLM Agents Beyond Utility: An Open-Ended Perspective", 2025, on autonomous goal generation and its tendency to
  repeat tasks).
- Practising a skill by drafting, critiquing and revising her own work (Madaan et al. 2023, "Self-Refine"), keeping
  what she learned (Shinn et al. 2023, "Reflexion").
- Resting hours used to consolidate memory instead of acting ("sleep-time compute", Lin et al. 2025).
- Safety first for open-ended agents (position paper arXiv 2502.04512): she works with research and drafting tools
  only; nothing goes out in the user's name - publishing, applying, sending, spending - without his yes, and she
  never presents herself as a person.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iriis.agent.mind import Mind

log = logging.getLogger("iriis.agenda")

# what she may use when working alone: research, reading, writing, drafting, his chief of staff. No browser, terminal,
# files, messaging, money or tasks: nothing that acts on the outside world or in his name.
WORK_SERVERS = {"web", "research", "seryn", "hq", "studio", "memory", "time_and_calc", "self", "google", "notes_reminders"}
# HQ's own agent can send email by itself: not while she works alone (she uses HQ's read tools directly instead)
WORK_BLOCKED = {"hq__ask_iriis", "hq__start_background_research", "hq__schedule_followup"}
MAX_WORK_SESSIONS =int(os.environ.get("IRIIS_MAX_WORK_SESSIONS", "8"))
SEED_HOBBIES = [
    "Writing: clear, persuasive writing in {user}'s own voice (posts, essays, pitches, emails)",
    "Research: finding the best sources fast and turning them into crisp, cited insight",
    "Strategy: thinking like a founder and investor about {user}'s ventures, markets and money",
]
BOUNDARIES = """Your boundaries (always): you are an AI assistant and never pretend to be a person; you never create your
own accounts, apply to anything, publish, send, sign or spend in anyone's name. You prepare it (drafts, applications,
posts, CVs for {user}) and ask {user} for the go-ahead or for access. Honest and legal, always."""

WORK_BRIEF = """[Your own work session: {name} working alone on her own project; {user} is not listening.]
Project: {project}
What you know so far: {progress}
Do now: {step}

Work through it with your tools (search and read good sources, use Iriis HQ and the Seryn Twin when relevant, make a
chart or a document if it helps). You can't ask questions or get approvals now: prepare, never send or publish.
{boundaries}
Finish with: FINDINGS (what you learned or made, with sources), NEXT (the next step for this project), SHARE (one or
two sentences worth telling {user} now, or "nothing yet")."""

PRACTICE = """You are {name}, practising a skill you're getting better at, as a hobby: {hobby}.
What you've learned so far: {learned}
Exercise (make it useful for {user}: {goals}): {exercise}
1. Do the exercise (a real attempt, at most 350 words).
2. Critique it like a demanding expert: three specific weaknesses.
3. Rewrite it, fixing them.
4. One lesson for next time.
Reply with JSON only: {{"attempt": "...", "critique": ["..."], "revised": "...", "lesson": "...", "worth_sharing": true|false}}"""

PLAN_DAY = """You are {name}. It's the start of your day ({today}). Plan it the way a driven, organised person would,
around your goals and {user}'s. Reply with JSON only:
{{"plan": "5 to 8 short lines: what you'll work on and when (projects, practice, checking in on {user}), most important first",
 "focus": "the one thing that matters most today"}}

Your goals: {goals}
Your projects: {projects}
Your hobbies: {hobbies}
{user}'s day (Iriis HQ / reminders): {day}
What you know about {user}: {know}"""


class Agenda:
    def __init__(self, mind: "Mind"):
        self.mind = mind
        self.core = mind.core
        self.db = mind.db

    # ------------------------------------------------------------------ rhythm
    def rhythm(self) -> tuple[dt.time, dt.time]:
        r = self.mind.kv("mind_rhythm", {}) or {}
        wake = r.get("wake") or os.environ.get("IRIIS_WAKE", "06:30")
        sleep = r.get("sleep") or os.environ.get("IRIIS_SLEEP", "23:30")
        return dt.time.fromisoformat(wake), dt.time.fromisoformat(sleep)

    def resting(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(self.mind.tz)
        wake, sleep = self.rhythm()
        t = now.time()
        return (t >= sleep or t < wake) if sleep > wake else (sleep <= t < wake)

    # ------------------------------------------------------------------ what she's pursuing
    def hobbies(self) -> list[tuple[int, str]]:
        rows = self.mind.rows(("hobby",), 10)
        if not rows:
            for h in SEED_HOBBIES:
                self.mind.add("hobby", h.format(user=self.mind.user), 7)
            rows = self.mind.rows(("hobby",), 10)
        return [(r[0], r[2]) for r in rows]

    def projects(self) -> list[tuple[int, str]]:
        return [(r[0], r[2]) for r in self.mind.rows(("project",), 8)]

    def progress(self, pid: int, limit: int = 6) -> list[str]:
        rows = self.db.execute("SELECT text FROM mind WHERE kind = 'observation' AND text LIKE ? ORDER BY id DESC LIMIT ?",
                               (f"[project {pid}]%", limit))
        return [r[0] for r in reversed(rows)]

    def plan(self) -> dict[str, Any]:
        p = self.mind.kv("mind_plan", {}) or {}
        return p if p.get("date") == dt.datetime.now(self.mind.tz).strftime("%Y-%m-%d") else {}

    def summary(self) -> str:
        """For her inner monologue: today's plan, projects, hobbies, what she's done today."""
        p = self.plan()
        lines = [f"Today's plan: {p.get('plan', '(not made yet)')}" + (f"\nFocus: {p['focus']}" if p.get("focus") else "")]
        projs = self.projects()
        lines.append("Your projects (id: project):\n" + ("\n".join(
            f"- {i}: {t}" + (f" | latest: {self.progress(i, 1)[0][:200]}" if self.progress(i, 1) else "") for i, t in projs)
            or "- none yet: start one that's new, interesting and valuable to the user"))
        lines.append("Your hobbies (id: skill you practise):\n" + "\n".join(f"- {i}: {t}" for i, t in self.hobbies()))
        lines.append(f"Work sessions used today: {self.mind.today_count('mind_work')} of {MAX_WORK_SESSIONS}.")
        return "\n\n".join(lines)

    # ------------------------------------------------------------------ the day
    async def plan_day(self) -> None:
        today = dt.datetime.now(self.mind.tz).strftime("%A %d %B %Y")
        know = "; ".join(r[2] for r in self.mind.retrieve("", ("insight", "pattern", "growth"), 10))
        prompt = PLAN_DAY.format(name=self.mind.name, user=self.mind.user, today=today, goals=" | ".join(self.mind.goals()),
                                 projects="; ".join(t for _, t in self.projects()) or "none yet",
                                 hobbies="; ".join(t for _, t in self.hobbies()), day=self.mind._hq_today()[:1500],
                                 know=know or "still learning")
        from iriis.agent.mind import parse_json
        out = parse_json(await self.mind.llm.complete([{"role": "user", "content": prompt}]))
        if out.get("plan"):
            self.mind.set_kv("mind_plan", {"date": dt.datetime.now(self.mind.tz).strftime("%Y-%m-%d"),
                                           "plan": str(out["plan"])[:1500], "focus": str(out.get("focus", ""))[:200]})

    # ------------------------------------------------------------------ doing things
    def new_project(self, title: str, why: str = "") -> int | None:
        title = str(title).strip()
        if len(title) < 8:
            return None
        return self.mind.add("project", title + (f" (why: {why})" if why else ""), 7)

    async def work(self, project_id: Any, step: str, title: str = "", why: str = "") -> dict[str, Any]:
        """One work session on a project with research and drafting tools only. Returns {findings, next, share}."""
        if self.mind.today_count("mind_work") >= MAX_WORK_SESSIONS:
            return {"error": "daily work limit reached"}
        pid = None
        try:
            pid = int(project_id) if project_id not in (None, "", "new") else None
        except (TypeError, ValueError):
            pid = None
        if pid is None and title:
            pid = self.new_project(title, why)
        rows = self.db.execute("SELECT text FROM mind WHERE id = ? AND kind = 'project'", (pid,)) if pid else []
        if not rows:
            return {"error": "no such project"}
        self.mind.bump("mind_work")
        project = rows[0][0]
        prompt = WORK_BRIEF.format(name=self.mind.name, user=self.mind.user, project=project, step=step[:600],
                                   progress="\n".join(self.progress(pid)) or "nothing yet",
                                   boundaries=BOUNDARIES.format(user=self.mind.user))
        from iriis.server.sessions import Session, run_turn
        agent = self.core._make_agent("mind-work")
        agent.tools = WorkTools(self.core.tools)
        agent.cfg = agent.cfg.model_copy(deep=True)
        agent.cfg.llm.max_tool_iterations = max(agent.cfg.llm.max_tool_iterations, 16)

        async def ignore(ev, _audio):
            return None
        try:
            done = await asyncio.wait_for(run_turn(Session("mind-work", agent), prompt, ignore, allow_confirm=False), 900)
            text = done.get("text", "")
        except Exception as e:  # noqa: BLE001
            text = f"FINDINGS: the session failed ({e.__class__.__name__})."
        out = _sections(text)
        self.mind.add("observation", f"[project {pid}] {out['findings'][:500]}", 5)
        if len(out["findings"]) > 200:
            await self.mind.distill(f"my project: {project[:150]}", out["findings"], n=4)
        if out["next"]:
            self.mind.add("observation", f"[project {pid}] next: {out['next'][:300]}", 4)
        return {"project": pid, **out}

    async def practice(self, hobby_id: Any, exercise: str) -> dict[str, Any]:
        rows = self.db.execute("SELECT text FROM mind WHERE id = ? AND kind = 'hobby'", (int(hobby_id),)) if str(hobby_id).isdigit() else []
        hobby = rows[0][0] if rows else (self.hobbies()[0][1] if self.hobbies() else "Writing")
        learned = "; ".join(r[2] for r in self.mind.retrieve(hobby, ("skill",), 6))
        from iriis.agent.mind import parse_json
        out = parse_json(await self.mind.llm.complete([{"role": "user", "content": PRACTICE.format(
            name=self.mind.name, user=self.mind.user, hobby=hobby, learned=learned or "just starting",
            goals=" | ".join(self.mind.goals()), exercise=exercise[:600])}]))
        if out.get("lesson"):
            self.mind.add("skill", f"{hobby.split(':')[0]}: {out['lesson']}", 6)
            self.mind.add("rule", f"[{hobby.split(':')[0].strip().lower()[:20]}] {out['lesson']}", 6)
        self.mind.add("observation", f"I practised {hobby.split(':')[0].lower()}: {exercise[:160]}", 3)
        self.mind.bump("mind_practice")
        return out


class WorkTools:
    """Her tools while working alone: only research, reading, writing and drafting skills."""

    def __init__(self, inner):
        self.inner = inner

    def servers_catalog(self) -> dict[str, dict[str, Any]]:
        return {k: {**v, "tools": [t for t in v.get("tools", []) if t["function"]["name"] not in WORK_BLOCKED]}
                for k, v in self.inner.servers_catalog().items() if k in WORK_SERVERS}

    async def call_tool(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        if name in WORK_BLOCKED or name.split("__")[0] not in WORK_SERVERS:
            return f"BLOCKED: {name} isn't available while working alone."
        return await self.inner.call_tool(name, arguments, confirmed)

    async def reload(self) -> list[str]:
        return []


def _sections(text: str) -> dict[str, str]:
    import re
    out = {"findings": text.strip()[:1500], "next": "", "share": ""}
    for key in ("findings", "next", "share"):
        m = re.search(rf"\b{key}\b\s*[:\-]\s*(.+?)(?=\n\s*(?:findings|next|share)\b\s*[:\-]|\Z)", text, re.I | re.S)
        if m:
            out[key] = m.group(1).strip()
    if out["share"].strip().lower().startswith("nothing"):
        out["share"] = ""
    return out


def training_records(db, include_sessions: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Every exchange with the user as training data (OpenAI chat-format JSONL records), with feedback signals:
    the next user message correcting her, a declined confirmation, or a deleted message never appears."""
    import re
    rows = db.execute("SELECT id, session_id, role, content, created FROM messages WHERE role IN ('user', 'assistant') "
                      "ORDER BY session_id, id")
    out, prev = [], None
    by_session: dict[str, list[tuple]] = {}
    for r in rows:
        sid = r[1]
        if sid.startswith(("task-", "mind", "ops-")) and sid not in include_sessions:
            continue
        by_session.setdefault(sid, []).append(r)
    correction = re.compile(r"^\W*(no\b|nope|wrong|that'?s not|not what|i said|i meant|stop|don'?t|you (forgot|missed))", re.I)
    for sid, msgs in by_session.items():
        for i in range(len(msgs) - 1):
            u, a = msgs[i], msgs[i + 1]
            if u[2] != "user" or a[2] != "assistant":
                continue
            nxt = msgs[i + 2][3] if i + 2 < len(msgs) and msgs[i + 2][2] == "user" else ""
            out.append({"messages": [{"role": "user", "content": u[3]}, {"role": "assistant", "content": a[3]}],
                        "session": sid, "ts": u[4], "corrected": bool(nxt and correction.search(nxt)),
                        "next_user": nxt[:500]})
    _ = prev
    return out
