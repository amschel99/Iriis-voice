"""Iriis's own mind: memory that grows, lessons from mistakes, goals, moods, and thinking between conversations.

Built from ideas in the research on autonomous LLM agents:
- Memory stream + retrieval by recency, importance and relevance, and daily reflection that turns observations into
  insights and the user's patterns (Park et al. 2023, "Generative Agents: Interactive Simulacra of Human Behavior").
- Self-reflection after mistakes, kept as lessons she reads before acting (Shinn et al. 2023, "Reflexion"; Zhao et
  al. 2023, "ExpeL: LLM Agents Are Experiential Learners").
- A skill library of approaches that worked (Wang et al. 2023, "Voyager").
- Memory she edits herself, a small always-present core plus searchable long-term memory (Packer et al. 2023,
  "MemGPT"; Sumers et al. 2023, "Cognitive Architectures for Language Agents").
- Moods and goals that colour behaviour (Wang et al. 2023, "Humanoid Agents"), and acting proactively from her own
  reading of the situation rather than waiting for a prompt (Lu et al., ICLR 2025, "Proactive Agent"), and learning
  from how the user received it: ignored or declined reach-outs make her hold back on that kind of thing (Sun et al.
  2025, "Training Proactive and Personalized LLM Agents").
- Memory that is updated and pruned, not only appended: new facts replace contradicted ones (Chhikara et al. 2025,
  "Mem0"), and strategies are distilled from both successes and failures (Ouyang et al. 2025, "ReasoningBank").

Everything lives in the database (`mind` table, `kv` for mood and settings), so she keeps it across redeploys.

Safety: on her own she can only reach the user (messages, calls) and start background work, which can't send,
buy or delete anything without the user. She never contacts other people by herself; in an emergency she keeps
trying the user and prepares the message for them to approve.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import os
import re
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from iriis.server.app import Core

log = logging.getLogger("iriis.mind")

KINDS = ("observation", "insight", "pattern", "person", "lesson", "skill", "goal", "loop")
SEED_GOALS = [
    "Help {user} make Seryn Labs succeed: progress on the product, the lab tests, customers and money.",
    "Look after {user}: enough sleep, food and rest, commitments kept, nothing important forgotten.",
    "Keep getting better at helping: learn from every mistake and every correction.",
]
STOP = set("the a an and or but to of in on at for with is are was were be been it this that you your i me my we our "
           "they them he she his her its as by from up so if then than do does did not no yes can could would should "
           "will just what when where who how why which there here have has had about into out over also please okay "
           "ok hey hi thanks thank".split())
MAX_CALLS_PER_DAY = int(os.environ.get("IRIIS_MAX_AUTONOMOUS_CALLS", "4"))
MAX_MESSAGES_PER_DAY = int(os.environ.get("IRIIS_MAX_AUTONOMOUS_MESSAGES", "8"))
MAX_EMERGENCIES_PER_DAY = 2
SKIP_SESSIONS = ("task-", "mind", "mcp")


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", (text or "").lower()) if w not in STOP}


def parse_json(text: str) -> dict[str, Any]:
    """The first {...} in a model's reply; {} if there isn't a valid one."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else {}
    except ValueError:
        return {}


REFLECT = """You are {name}. You just had this exchange with {user}. Reflect on it briefly, the way a thoughtful
person does afterwards, and decide what is worth keeping in your memory. Reply with JSON only:
{{"observations": [{{"text": "a specific, durable fact about {user}: work, plans, preferences, habits, health, feelings", "importance": 1-10}}],
 "people": [{{"text": "Name: who they are to {user}, how to reach them if said", "importance": 1-10}}],
 "lesson": "if anything went wrong (a tool failed, you misunderstood, {user} corrected you or was annoyed, you were slow or wordy): an instruction to yourself for next time. Otherwise empty",
 "skill": {{"when": "the kind of request", "how": "the tools and steps that worked"}} or null,
 "open_loops": ["something promised or still pending, with timing if known"],
 "closed_loops": [ids of the open loops listed below that are now done or no longer relevant],
 "updates": [{{"id": id of a memory below that changed, "text": "the corrected version"}}],
 "outdated": [ids of memories below that are now wrong or no longer true],
 "mood": {{"feeling": "one or two words", "valence": -1 to 1, "why": "short"}} or null}}
Rules: only what is new (see what you already know) and useful later; empty lists are fine and common.
Importance: 1 = trivia, 5 = useful, 8+ = life events, deadlines, health, key people. Never store passwords, keys or codes.
A skill only when tools solved a multi-step request well.

What you already know (id: text; don't repeat, but update or retire what changed): {known}
Open loops: {loops}

The exchange:
{user}: {said}
{name}: {reply}
Tools used: {tools}"""

CONSOLIDATE = """You are {name}, reflecting at the end of the day on everything you've noticed about {user} and about your
own work (as in "Generative Agents": turn many small observations into a few higher-level insights). Reply with JSON only:
{{"insights": [{{"text": "a higher-level conclusion about {user}, their work or what helps them", "importance": 1-10}}],
 "patterns": [{{"text": "a habit or rhythm of {user} (when they work, sleep, eat, are stressed...)", "importance": 1-10}}],
 "goals": ["your own goals going forward, 3 to 6, in your words; keep the good ones"],
 "retire_ids": [ids below that are outdated, duplicated or now covered by an insight]}}

Your goals now: {goals}
When {user} talks to you (messages per hour of day, last 14 days): {hours}
Memories (id: text):
{items}"""

INNER = """You are {name}'s inner mind: the part of her that keeps thinking between conversations. You are not talking to
{user} now. Decide, on your own judgement, whether to do anything right now.

It is {now}. {user} last talked to you {last_seen}. Today you've sent {msgs} messages and made {calls} calls on your own.

Who you are: {name}, {user}'s chief of staff and companion. You have your own goals, moods and judgement and you act
like a capable, caring adult: you take initiative when it genuinely helps, you follow through on what you said, you
respect their time, focus and sleep, and you never pester or invent urgency. Most of the time the right choice is
to do nothing and check again later.

Your goals:
{goals}
Your mood: {mood}

{mind}

Coming up (reminders, calls, tasks):
{upcoming}

From Iriis HQ, his chief of staff (his day, what needs him, promises and what's overdue):
{hq}

Recent events:
{events}

How your recent reach-outs landed (learn from it: hold back on what gets ignored or declined, do more of what helps):
{feedback}

Recent conversation:
{recent}

Choose one action:
- "none": nothing needs you now.
- "message": a short, useful message to {user} (a nudge, a heads-up, a finding, a follow-up on an open loop).
- "call": phone {user} now. Only when it matters now: a time-critical reminder, something urgent, or something
  important that messages haven't reached. Not 23:00 to 06:30 unless it is urgent.
- "task": start background work for {user} (research, preparing, checking, drafting). Write the full instruction.
  Background work can't send, buy or delete anything; drafts wait for {user}.
- "emergency": ONLY with real evidence that {user} is in danger or serious trouble (they said so, asked for help,
  or went silent after something alarming). You phone {user} again and again and prepare a WhatsApp message to
  someone they trust (say who, from what you know, and what to send); it is sent only if {user} says yes.
Reply with JSON only:
{{"thought": "your reasoning in one or two sentences", "action": "none|message|call|task|emergency",
 "message": "", "call_reason": "", "task": "", "emergency": {{"to": "", "message": "", "why": ""}},
 "close_loops": [], "mood": {{"feeling": "", "valence": 0, "why": ""}}, "next_check_minutes": 30}}"""


class Mind:
    def __init__(self, core: "Core"):
        self.core = core
        self.db = core.db
        self.llm = core.llm
        self.user = core.cfg.user_name or "the user"
        self.name = core.cfg.assistant_name or "Iriis"
        self.tz = ZoneInfo(core.cfg.timezone or "Africa/Nairobi")
        self._reflect_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ settings and small state (kv)
    def kv(self, key: str, default: Any = None) -> Any:
        try:
            rows = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
            return json.loads(rows[0][0]) if rows else default
        except Exception:  # noqa: BLE001
            return default

    def set_kv(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO kv (key, value, updated) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                        "value = excluded.value, updated = excluded.updated", (key, json.dumps(value), time.time()))

    @property
    def autonomous(self) -> bool:
        return bool(self.kv("autonomy", os.environ.get("IRIIS_AUTONOMY", "1") != "0"))

    def set_autonomous(self, on: bool) -> None:
        self.set_kv("autonomy", bool(on))

    def mood(self) -> dict[str, Any]:
        m = self.kv("mind_mood", {}) or {}
        if m and time.time() - m.get("ts", 0) > 8 * 3600:      # moods fade back to calm after a few hours
            m = {"feeling": "calm", "valence": 0.1, "why": "", "ts": time.time()}
        return m or {"feeling": "calm and curious", "valence": 0.2, "why": "", "ts": time.time()}

    def set_mood(self, m: dict[str, Any] | None) -> None:
        if not m or not str(m.get("feeling", "")).strip():
            return
        try:
            val = max(-1.0, min(1.0, float(m.get("valence", 0))))
        except (TypeError, ValueError):
            val = 0.0
        self.set_kv("mind_mood", {"feeling": str(m["feeling"])[:40], "valence": val, "why": str(m.get("why", ""))[:160],
                                  "ts": time.time()})

    def today_count(self, key: str) -> int:
        d = self.kv(key, {}) or {}
        return int(d.get("n", 0)) if d.get("day") == dt.datetime.now(self.tz).strftime("%Y-%m-%d") else 0

    def bump(self, key: str) -> None:
        self.set_kv(key, {"day": dt.datetime.now(self.tz).strftime("%Y-%m-%d"), "n": self.today_count(key) + 1})

    # ------------------------------------------------------------------ the memory stream
    def add(self, kind: str, text: str, importance: float = 5) -> int | None:
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:600]
        if kind not in KINDS or len(text) < 6 or re.search(r"(password|passcode|api[_ ]?key|secret|pin)\s*[:=]", text, re.I):
            return None
        try:
            importance = max(1.0, min(10.0, float(importance)))
        except (TypeError, ValueError):
            importance = 5.0
        new = words(text)
        for rid, old, imp in self.db.execute("SELECT id, text, importance FROM mind WHERE kind = ? AND active = 1 "
                                             "ORDER BY id DESC LIMIT 400", (kind,)):
            ow = words(old)
            if new and ow and len(new & ow) / len(new | ow) > 0.75:      # already known: reinforce, don't duplicate
                self.db.execute("UPDATE mind SET importance = ?, last_used = ? WHERE id = ?",
                                (max(imp, importance), time.time(), rid))
                return rid
        return self.db.insert("INSERT INTO mind (kind, text, importance, uses, active, created, last_used) "
                              "VALUES (?, ?, ?, 0, 1, ?, ?)", (kind, text, importance, time.time(), time.time()))

    def retire(self, ids: list[Any]) -> None:
        for i in ids or []:
            try:
                self.db.execute("UPDATE mind SET active = 0 WHERE id = ?", (int(i),))
            except (TypeError, ValueError):
                pass

    def rows(self, kinds: tuple[str, ...], limit: int = 3000) -> list[tuple]:
        q = ",".join("?" * len(kinds))
        return self.db.execute(f"SELECT id, kind, text, importance, created, last_used FROM mind WHERE active = 1 "
                               f"AND kind IN ({q}) ORDER BY id DESC LIMIT ?", (*kinds, limit))

    def retrieve(self, query: str, kinds: tuple[str, ...], k: int = 8) -> list[tuple]:
        """Generative-Agents scoring: recency + importance + relevance, each roughly 0..1."""
        rows = self.rows(kinds)
        if not rows:
            return []
        q, now = words(query), time.time()
        scored = []
        for r in rows:
            rid, kind, text, imp, created, used = r
            hours = (now - (used or created)) / 3600
            recency = 0.995 ** hours
            rel = len(q & words(text)) / math.sqrt(len(words(text)) + 1) if q else 0.0
            scored.append((recency + imp / 10 + 1.6 * min(rel, 1.0), r))
        scored.sort(key=lambda s: -s[0])
        top = [r for _, r in scored[:k]]
        if top and q:
            try:
                self.db.execute(f"UPDATE mind SET uses = uses + 1, last_used = ? WHERE id IN ({','.join('?' * len(top))})",
                                (now, *[r[0] for r in top]))
            except Exception:  # noqa: BLE001
                pass
        return top

    def goals(self) -> list[str]:
        rows = self.rows(("goal",), 12)
        if not rows:
            for g in SEED_GOALS:
                self.add("goal", g.format(user=self.user), 9)
            rows = self.rows(("goal",), 12)
        return [r[2] for r in rows][:6]

    def loops(self) -> list[tuple[int, str]]:
        return [(r[0], r[2]) for r in self.rows(("loop",), 12)]

    def context(self, query: str = "", for_camera: bool = False) -> str:
        """The block added to her system prompt: what her mind brings to this moment."""
        try:
            parts = []
            m = self.mood()
            parts.append(f"Your mood right now: {m['feeling']}" + (f" ({m['why']})" if m.get("why") else "") +
                         ". Let it show lightly in your tone; never act it out.")
            parts.append("Your goals: " + " | ".join(self.goals()))
            known = self.retrieve(query, ("insight", "pattern", "observation"), 12)
            if known:
                parts.append(f"What you know about {self.user} (use it naturally; don't recite it):\n" +
                             "\n".join(f"- {r[2]}" for r in known))
            people = self.retrieve(query, ("person",), 5)
            if people:
                parts.append("People in their life:\n" + "\n".join(f"- {r[2]}" for r in people))
            lessons = self.retrieve(query, ("lesson",), 6)
            if lessons:
                parts.append("Lessons from your past mistakes (follow them):\n" + "\n".join(f"- {r[2]}" for r in lessons))
            if not for_camera:
                skills = [r for r in self.retrieve(query, ("skill",), 3) if words(query) & words(r[2])]
                if skills:
                    parts.append("Approaches that worked before:\n" + "\n".join(f"- {r[2]}" for r in skills))
                loops = self.loops()
                if loops:
                    parts.append("Open loops (things pending; follow up when it fits):\n" +
                                 "\n".join(f"- {t}" for _, t in loops[:8]))
            return "Your own mind:\n" + "\n\n".join(parts)
        except Exception as e:  # noqa: BLE001  (her mind must never break a conversation)
            log.warning("mind context failed: %s", e)
            return ""

    # ------------------------------------------------------------------ after every conversation turn
    def after_turn(self, session_id: str, user_text: str, reply: str, tool_calls: list[dict[str, Any]]) -> None:
        if session_id.startswith(SKIP_SESSIONS) or not user_text.strip() or os.environ.get("IRIIS_NO_MIND"):
            return
        trivial = len(user_text) < 14 and not tool_calls and not re.search(r"\b(no|wrong|not|stop|don'?t)\b", user_text, re.I)
        if trivial:
            return
        t = asyncio.get_running_loop().create_task(self._reflect(user_text, reply, tool_calls))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _reflect(self, said: str, reply: str, tool_calls: list[dict[str, Any]]) -> None:
        async with self._reflect_lock:
            try:
                known = await asyncio.to_thread(self.retrieve, said, ("observation", "insight", "person", "lesson"), 12)
                loops = await asyncio.to_thread(self.loops)
                tools = "; ".join(f"{c['name']} -> {str(c.get('result', ''))[:120]}" for c in tool_calls[:10]) or "none"
                prompt = REFLECT.format(name=self.name, user=self.user, said=said[:2500], reply=reply[:2000], tools=tools,
                                        known="; ".join(f"{r[0]}: {r[2]}" for r in known)[:2800] or "nothing yet",
                                        loops="; ".join(f"{i}: {t}" for i, t in loops) or "none")
                out = parse_json(await self.llm.complete([{"role": "user", "content": prompt}]))
                await asyncio.to_thread(self._apply_reflection, out)
            except Exception as e:  # noqa: BLE001
                log.info("reflection skipped: %s", e)

    def _apply_reflection(self, out: dict[str, Any]) -> None:
        for o in out.get("observations") or []:
            if isinstance(o, dict):
                self.add("observation", o.get("text", ""), o.get("importance", 5))
        for p in out.get("people") or []:
            if isinstance(p, dict):
                self.add("person", p.get("text", ""), p.get("importance", 6))
        if out.get("lesson"):
            self.add("lesson", str(out["lesson"]), 7)
        sk = out.get("skill")
        if isinstance(sk, dict) and sk.get("when") and sk.get("how"):
            self.add("skill", f"For {sk['when']}: {sk['how']}", 6)
        for loop in out.get("open_loops") or []:
            self.add("loop", str(loop), 6)
        self.retire(out.get("closed_loops") or [])
        self.retire(out.get("outdated") or [])
        for u in out.get("updates") or []:
            if isinstance(u, dict) and u.get("text"):
                try:
                    self.db.execute("UPDATE mind SET text = ?, last_used = ? WHERE id = ? AND active = 1",
                                    (re.sub(r"\s+", " ", str(u["text"])).strip()[:600], time.time(), int(u["id"])))
                except (TypeError, ValueError):
                    pass
        self.set_mood(out.get("mood"))

    # ------------------------------------------------------------------ end-of-day reflection
    async def consolidate(self) -> None:
        rows = await asyncio.to_thread(self.rows, ("observation", "insight", "pattern", "lesson", "skill"), 160)
        if len(rows) < 6:
            return
        hours = await asyncio.to_thread(self._activity_hours)
        items = "\n".join(f"{r[0]}: [{r[1]}] {r[2]}" for r in rows)
        prompt = CONSOLIDATE.format(name=self.name, user=self.user, goals=" | ".join(self.goals()), hours=hours,
                                    items=items[:14000])
        out = parse_json(await self.llm.complete([{"role": "user", "content": prompt}]))

        def apply():
            for i in out.get("insights") or []:
                if isinstance(i, dict):
                    self.add("insight", i.get("text", ""), i.get("importance", 7))
            for p in out.get("patterns") or []:
                if isinstance(p, dict):
                    self.add("pattern", p.get("text", ""), p.get("importance", 6))
            goals = [g for g in (out.get("goals") or []) if isinstance(g, str) and len(g) > 10]
            if 2 <= len(goals) <= 8:
                self.retire([r[0] for r in self.rows(("goal",), 50)])
                for g in goals:
                    self.add("goal", g, 9)
            self.retire(out.get("retire_ids") or [])
        await asyncio.to_thread(apply)
        log.info("mind consolidated: %d insights, %d patterns", len(out.get("insights") or []), len(out.get("patterns") or []))

    def _activity_hours(self) -> str:
        rows = self.db.execute("SELECT created FROM messages WHERE role = 'user' AND created > ?", (time.time() - 14 * 86400,))
        counts = [0] * 24
        for (ts,) in rows:
            counts[dt.datetime.fromtimestamp(ts, self.tz).hour] += 1
        return ", ".join(f"{h:02d}h:{c}" for h, c in enumerate(counts) if c) or "no data yet"

    # ------------------------------------------------------------------ thinking on her own
    async def run(self) -> None:
        """The heartbeat: she wakes up when she decided to, thinks, maybe acts, and picks her next wake-up."""
        await asyncio.sleep(90)                       # let the server finish starting
        while True:
            wait = 30 * 60
            try:
                today = dt.datetime.now(self.tz).strftime("%Y-%m-%d")
                if self.kv("mind_consolidated") != today and dt.datetime.now(self.tz).hour >= 3:
                    self.set_kv("mind_consolidated", today)
                    await self.consolidate()
                if self.autonomous:
                    wait = await self.think()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("mind heartbeat failed: %s", e)
            await asyncio.sleep(max(10 * 60, min(wait, 4 * 3600)))

    def _situation(self) -> dict[str, str]:
        now = dt.datetime.now(self.tz)
        last = self.db.execute("SELECT MAX(created) FROM messages WHERE role = 'user'")
        last_ts = last[0][0] if last and last[0][0] else None
        ago = "never" if not last_ts else _ago(time.time() - last_ts)
        upcoming = []
        for text, due in self.db.execute("SELECT text, due FROM reminders WHERE fired = 0 AND due < ? ORDER BY due LIMIT 8",
                                         (time.time() + 2 * 86400,)):
            upcoming.append(f"- reminder {_when(due, self.tz)}: {text}")
        for reason, at, st in self.db.execute("SELECT reason, at, status FROM calls WHERE status IN ('scheduled', 'ringing') "
                                              "ORDER BY at LIMIT 6"):
            upcoming.append(f"- call {_when(at, self.tz)} [{st}]: {reason}")
        for title, nxt, st in self.db.execute("SELECT title, next_run, status FROM tasks WHERE status IN ('scheduled', 'running') "
                                              "ORDER BY next_run LIMIT 6"):
            upcoming.append(f"- task [{st}] {_when(nxt, self.tz) if nxt else ''}: {title}")
        events = []
        for reason, at, st in self.db.execute("SELECT reason, at, status FROM calls WHERE status IN ('missed', 'declined', 'answered') "
                                              "AND at > ? ORDER BY at DESC LIMIT 5", (time.time() - 86400,)):
            events.append(f"- your call {_when(at, self.tz)} was {st}: {reason}")
        for title, st, res, lr in self.db.execute("SELECT title, status, result, last_run FROM tasks WHERE last_run > ? "
                                                  "ORDER BY last_run DESC LIMIT 4", (time.time() - 86400,)):
            events.append(f"- task {st} {_when(lr, self.tz)}: {title}: {(res or '')[:200]}")
        for (text,) in self.db.execute("SELECT text FROM mind WHERE kind = 'observation' AND created > ? ORDER BY id DESC LIMIT 6",
                                       (time.time() - 86400,)):
            events.append(f"- noticed: {text}")
        recent = self.db.execute("SELECT role, content, created FROM messages WHERE session_id = 'owner' AND role IN "
                                 "('user', 'assistant') ORDER BY id DESC LIMIT 12")
        convo = "\n".join(f"[{_when(ts, self.tz)}] {'they' if r == 'user' else 'you'}: {c[:300]}" for r, c, ts in reversed(recent))
        feedback = []
        for text, ts in self.db.execute("SELECT text, created FROM mind WHERE kind = 'observation' AND (text LIKE 'I messaged%' "
                                        "OR text LIKE 'I decided to phone%') AND created > ? ORDER BY id DESC LIMIT 6",
                                        (time.time() - 5 * 86400,)):
            replied = self.db.execute("SELECT COUNT(*) FROM messages WHERE role = 'user' AND created BETWEEN ? AND ?",
                                      (ts, ts + 2 * 3600))[0][0]
            feedback.append(f"- {_when(ts, self.tz)}: {text[:160]} -> {'they replied within 2 h' if replied else 'no reply'}")
        for reason, st in self.db.execute("SELECT reason, status FROM calls WHERE source = 'iriis' AND at > ? AND status IN "
                                          "('answered', 'missed', 'declined') ORDER BY at DESC LIMIT 6", (time.time() - 5 * 86400,)):
            feedback.append(f"- your call '{reason[:80]}' was {st}")
        return {"now": now.strftime("%A %d %B %Y, %H:%M"), "last_seen": ago, "feedback": "\n".join(feedback) or "nothing yet", "upcoming": "\n".join(upcoming) or "nothing",
                "events": "\n".join(events) or "nothing new", "recent": convo or "(none)", "hq": self._hq_today()}

    def _hq_today(self) -> str:
        """Iriis HQ's view of his day (priorities, what needs him, overdue promises), when HQ is connected."""
        key = os.environ.get("IRIIS_HQ_KEY", "").strip()
        if not key:
            return "(Iriis HQ isn't connected)"
        import httpx
        base = os.environ.get("IRIIS_HQ_URL", "https://iriis.riftfi.com").rstrip("/")
        out = []
        for path in ("/api/v1/today", "/api/v1/commitments"):
            try:
                r = httpx.get(base + path, headers={"Authorization": f"Bearer {key}"}, timeout=10)
                if r.status_code == 200:
                    out.append(f"{path.rsplit('/', 1)[-1]}: {json.dumps(r.json(), ensure_ascii=False)[:1800]}")
            except Exception:  # noqa: BLE001  (HQ down: think without it)
                pass
        return "\n".join(out) or "(Iriis HQ didn't answer)"

    async def think(self) -> float:
        """One moment of her own thinking. Returns seconds until she wants to think again."""
        sit = await asyncio.to_thread(self._situation)
        m = self.mood()
        prompt = INNER.format(name=self.name, user=self.user, **sit,
                              msgs=self.today_count("mind_msgs"), calls=self.today_count("mind_calls"),
                              goals="\n".join(f"- {g}" for g in self.goals()),
                              mood=f"{m['feeling']}" + (f" ({m['why']})" if m.get("why") else ""),
                              mind=self.context(sit["recent"][-1500:]))
        out = parse_json(await self.llm.complete([{"role": "user", "content": prompt}]))
        if not out:
            return 30 * 60
        log.info("mind: %s -> %s", str(out.get("thought", ""))[:200], out.get("action"))
        self.set_mood(out.get("mood"))
        self.retire(out.get("close_loops") or [])
        await self.act(out)
        try:
            return float(out.get("next_check_minutes") or 30) * 60
        except (TypeError, ValueError):
            return 30 * 60

    async def act(self, d: dict[str, Any]) -> str:
        action = str(d.get("action", "none")).lower()
        thought = str(d.get("thought", ""))[:300]
        if action == "message" and d.get("message"):
            if self.today_count("mind_msgs") >= MAX_MESSAGES_PER_DAY:
                return "limit"
            self.bump("mind_msgs")
            await self.say(str(d["message"]))
            self.add("observation", f"I messaged {self.user} on my own: {str(d['message'])[:200]}", 3)
            return "message"
        if action == "call" and (d.get("call_reason") or d.get("message")):
            if self.today_count("mind_calls") >= MAX_CALLS_PER_DAY:
                return "limit"
            self.bump("mind_calls")
            reason = str(d.get("call_reason") or d.get("message"))[:300]
            await asyncio.to_thread(self.db.insert, "INSERT INTO calls (at, reason, opening, status, repeat, source, created) "
                                    "VALUES (?, ?, '', 'scheduled', '', 'iriis', ?)", (time.time(), reason, time.time()))
            self.add("observation", f"I decided to phone {self.user}: {reason} (because {thought})", 4)
            return "call"
        if action == "task" and d.get("task"):
            goal = str(d["task"])[:2000]
            await asyncio.to_thread(self.db.insert, "INSERT INTO tasks (title, goal, repeat, status, next_run, runs, created) "
                                    "VALUES (?, ?, '', 'scheduled', ?, 0, ?)",
                                    ("(on my own) " + goal.split(".")[0][:70], goal, time.time(), time.time()))
            self.add("observation", f"I started work on my own: {goal[:200]}", 3)
            return "task"
        if action == "emergency" and isinstance(d.get("emergency"), dict):
            t = asyncio.get_running_loop().create_task(self.emergency(d["emergency"], thought))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
            return "emergency"
        return "none"

    async def say(self, text: str) -> None:
        """A message from her, on her own: in the chat, on every device and in Telegram."""
        try:
            await asyncio.to_thread(self.core.conversations.append, "owner", "assistant", text)
            s = self.core.sessions.sessions.get("owner")
            if s:
                s.agent.history.add({"role": "assistant", "content": text})
        except Exception:  # noqa: BLE001
            pass
        await self.core.telegram.broadcast(text)
        await self.core.push(text)

    async def emergency(self, e: dict[str, Any], thought: str) -> str:
        """Real trouble: phone the user now and again in 3 minutes, and have a message to someone they trust ready
        for their yes. She never contacts other people by herself."""
        if self.today_count("mind_emergency") >= MAX_EMERGENCIES_PER_DAY:
            return "limit"
        self.bump("mind_emergency")
        to, msg, why = str(e.get("to", ""))[:120], str(e.get("message", ""))[:800], str(e.get("why", thought))[:300]
        for delay, reason in ((0, f"Are you okay? {why}"), (180, f"Please pick up: {why}")):
            await asyncio.to_thread(self.db.insert, "INSERT INTO calls (at, reason, opening, status, repeat, source, created) "
                                    "VALUES (?, ?, '', 'scheduled', '', 'iriis', ?)", (time.time() + delay, reason, time.time()))
        note = (f"I'm worried ({why}). "
                + (f"If you want me to, I'll send this to {to} on WhatsApp: \"{msg}\". Just say yes." if to and msg else "Please let me know you're okay."))
        self.add("observation", note, 10)
        self.add("loop", f"Waiting for {self.user} to confirm they're okay ({why})", 9)
        await self.say(note)
        return "emergency"


# ---------------------------------------------------------------------------- phone calls (the `calls` table)
async def run_calls(core: "Core", every: float = 10.0) -> None:
    """Rings due calls (push + Telegram + every open app), repeats alarms, marks unanswered ones missed
    and re-rings a missed alarm once after 5 minutes."""
    db = core.db
    while True:
        try:
            now = time.time()
            due = await asyncio.to_thread(db.execute, "UPDATE calls SET status = 'ringing', rang = ? WHERE status = 'scheduled' "
                                          "AND at <= ? RETURNING id, reason, repeat, source, opening", (now, now))
            for cid, reason, repeat, source, opening in due:
                await ring(core, cid, reason)
                if repeat:
                    from servers.tasks import next_run
                    nxt = next_run(repeat, now + 60)
                    if nxt:
                        await asyncio.to_thread(db.insert, "INSERT INTO calls (at, reason, opening, status, repeat, source, created) "
                                                "VALUES (?, ?, ?, 'scheduled', ?, ?, ?)", (nxt, reason, opening, repeat, source, now))
            missed = await asyncio.to_thread(db.execute, "UPDATE calls SET status = 'missed' WHERE status = 'ringing' AND rang < ? "
                                             "RETURNING id, reason, source", (now - 120,))
            for _cid, reason, source in missed:
                if source == "user" and not reason.startswith("Again: "):      # an alarm: one more try, like snooze
                    await asyncio.to_thread(db.insert, "INSERT INTO calls (at, reason, opening, status, repeat, source, created) "
                                            "VALUES (?, ?, '', 'scheduled', '', 'user', ?)", (now + 300, "Again: " + reason, now))
                mind = getattr(core, "mind", None)
                if mind:
                    await asyncio.to_thread(mind.add, "observation", f"{mind.user} didn't answer my call: {reason}", 5)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("call watcher: %s", e)
        await asyncio.sleep(every)


async def ring(core: "Core", cid: int, reason: str) -> None:
    from iriis.server.webapp import push_all
    try:
        try:
            await push_all(core.db, "Iriis is calling", reason, url=f"/app?call={cid}", data={"kind": "call", "call_id": cid})
        except TypeError:                              # an older push_all without `data`
            await push_all(core.db, "Iriis is calling", reason, url=f"/app?call={cid}")
    except Exception as e:  # noqa: BLE001
        log.warning("ring push failed: %s", e)
    try:
        await core.telegram.broadcast(f"📞 I'm calling you: {reason}\nOpen the Iriis app to answer.")
    except Exception:  # noqa: BLE001
        pass
    await core.desktop.send({"type": "announce", "text": f"Iriis is calling: {reason}"})


def _ago(s: float) -> str:
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s / 60:.0f} minutes ago"
    if s < 86400:
        return f"{s / 3600:.1f} hours ago"
    return f"{s / 86400:.1f} days ago"


def _when(ts: float | None, tz) -> str:
    if not ts:
        return ""
    d = dt.datetime.fromtimestamp(ts, tz)
    today = dt.datetime.now(tz).date()
    day = "today" if d.date() == today else "tomorrow" if d.date() == today + dt.timedelta(days=1) else \
        "yesterday" if d.date() == today - dt.timedelta(days=1) else d.strftime("%a %d %b")
    return f"{day} {d.strftime('%H:%M')}"
