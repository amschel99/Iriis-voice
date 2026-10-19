"""The agent loop: user turn -> LLM (streaming) -> tool calls with permission checks -> repeat."""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from iriis.agent.conversations import OWNER_SESSION, SUMMARY_PROMPT, ConversationStore, format_dialogue
from iriis.agent.memory import ConversationHistory
from iriis.agent.permissions import STICKY, STICKY_S, AuditLog, PermissionManager, describe_call, is_affirmative, redact_secrets
from iriis.agent.tool_router import ToolRouter
from iriis.config import Config
from iriis.llm.base import LLMClient, ToolCall
from iriis.llm.prompts import FILLERS, build_system_prompt, wrap_tool_result
from iriis.llm.sentences import SentenceChunker

MAX_TOOL_RESULT_CHARS = 4000
SCREEN_MARKER = re.compile(r"\bSCREEN:\s*", re.I)

RESET_RE = re.compile(r"^\W*(start over|new conversation|reset( the)? conversation|forget (this|our) conversation)\W*$", re.I)
RELOAD_RE = re.compile(r"^\W*reload (the |your )?tools\W*$", re.I)


def _is_silent(text: str) -> bool:
    """The model's way of ignoring speech that wasn't meant for her."""
    return re.sub(r"[^a-z]", "", (text or "").lower()) == "silent"


class ToolProvider(Protocol):
    def servers_catalog(self) -> dict[str, dict[str, Any]]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str: ...
    async def reload(self) -> list[str]: ...


@dataclass
class TurnCallbacks:
    """How the agent talks to the outside world (console, TTS, mic)."""
    say: Callable[[str], Awaitable[None]]                  # a speakable sentence
    confirm: Callable[[str], Awaitable[str]]               # ask a question, return the user's live reply
    show: Callable[[str, str], None] = lambda kind, text: None  # UI-only output: ("tool", ...), ("screen", ...)
    on_first_token: Callable[[], None] = lambda: None
    heard: Callable[[], str] | None = None   # on interruption: the words the user actually heard


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)



TURN_BUDGET_CHARS = 60_000   # one turn's tool results (a long research task) before older ones are shortened
KEEP_WHOLE = 3               # the most recent tool results stay whole


def compact_turn(turn: list[dict[str, Any]], budget: int = TURN_BUDGET_CHARS, keep: int = KEEP_WHOLE) -> int:
    """Within one turn, every page read stays in context. Past `budget` characters, shorten the older tool results
    (head kept, marked) so long tasks stay fast and inside the model's context. Returns characters removed."""
    tools = [m for m in turn if m.get("role") == "tool" and isinstance(m.get("content"), str)]
    total = sum(len(m["content"]) for m in turn if isinstance(m.get("content"), str))
    removed = 0
    for m in tools[:-keep] if keep else tools:
        if total - removed <= budget:
            break
        c = m["content"]
        if len(c) > 1600 and not c.endswith("[shortened to save space]"):
            m["content"] = c[:1500] + "\n[shortened to save space]"
            removed += len(c) - len(m["content"])
    return removed

MORE_TOOLS_NAME = "more_tools"
MORE_TOOLS = {"type": "function", "function": {
    "name": MORE_TOOLS_NAME,
    "description": ("Only part of your tools are loaded each turn. If you need a skill that isn't in your list (the "
                    "browser, WhatsApp, the terminal, files, email, Seryn, money, documents...), call this with what "
                    "you need, e.g. 'run a terminal command' or 'browser'; those tools are added at once. Never tell "
                    "the user something is unavailable before trying this."),
    "parameters": {"type": "object", "properties": {"need": {"type": "string", "description": "the job or skill"}},
                   "required": ["need"]}}}


class Agent:
    def __init__(self, cfg: Config, llm: LLMClient, tools: ToolProvider | None,
                 permissions: PermissionManager, memory_facts: Callable[[], list[str]] | None = None,
                 store: ConversationStore | None = None, session_id: str = OWNER_SESSION):
        self.cfg = cfg
        self.llm = llm
        self.tools = tools
        self.permissions = permissions
        self.audit = AuditLog(cfg.permissions.audit_log)
        self.router = ToolRouter(cfg.tool_routing.max_tools, cfg.tool_routing.method)
        self.history = ConversationHistory(cfg.llm.history_max_tokens)
        self.memory_facts = memory_facts or (lambda: [])
        # Persistent conversation: reload recent context (survives restarts and redeploys).
        self.store = store
        self.session_id = session_id
        self.summary = ""
        self.last_active: float | None = None
        self._summarizing: asyncio.Task | None = None
        if store:
            try:
                self.history.messages = store.recent(session_id, cfg.llm.history_max_tokens)
                self.summary = store.summary(session_id)
                self.last_active = store.last_active(session_id)
            except Exception:  # noqa: BLE001  (database down: start with empty context)
                pass

    # ------------------------------------------------------------------ helpers

    def _system(self) -> dict[str, Any]:
        try:
            facts = self.memory_facts()
        except Exception:
            facts = []
        prompt = build_system_prompt(
            self.cfg.assistant_name, self.cfg.user_name, self.cfg.location, self.cfg.timezone, facts,
            conversation_summary=self.summary, last_active=self.last_active,
            casting=self.cfg.tts.casting(), voice_style=self._voice_style())
        note = getattr(self, "tools_note", "")
        return {"role": "system", "content": prompt + ("\n\n" + note if note else "")}

    def _voice_style(self) -> str:
        """Which tag syntax she should write: only what the voice engine in use can actually perform."""
        return self.cfg.tts.effective_flavor() or "plain"

    # ------------------------------------------------------------------ persistence

    def revise_last_reply(self, content: str) -> None:
        """The user interrupted after the reply was written but before they'd heard all of it."""
        for m in reversed(self.history.messages):
            if m.get("role") == "assistant" and not m.get("tool_calls"):
                m["content"] = content
                break
        if self.store:
            try:
                self.store.amend_last_reply(self.session_id, content)
            except Exception:  # noqa: BLE001
                pass

    def _save(self, user_text: str, reply: str) -> None:
        if not self.store:
            return
        try:
            self.store.append(self.session_id, "user", user_text)
            self.store.append(self.session_id, "assistant", reply)
        except Exception:  # noqa: BLE001  (never lose a turn because the DB hiccuped)
            pass

    def _maybe_summarize(self) -> None:
        """Fold messages that fell out of the context window into the rolling summary (background)."""
        if not self.store or len(self.history.dropped) < 4 or (self._summarizing and not self._summarizing.done()):
            return
        dropped, self.history.dropped = self.history.dropped, []

        async def run():
            prompt = SUMMARY_PROMPT.format(name=self.cfg.assistant_name, summary=self.summary or "(none yet)",
                                           dialogue=format_dialogue(dropped))
            try:
                text = (await self.llm.complete([{"role": "user", "content": prompt}])).strip()
                if text:
                    self.summary = text
                    await asyncio.to_thread(self.store.set_summary, self.session_id, text)
            except Exception:  # noqa: BLE001
                self.history.dropped = dropped + self.history.dropped   # retry next turn

        self._summarizing = asyncio.get_running_loop().create_task(run())

    async def _llm_pick_servers(self, query: str, catalog: dict[str, str]) -> list[str]:
        listing = "\n".join(f"{n}: {d}" for n, d in catalog.items())
        prompt = (f"Which of these tool groups are needed to handle the request? Reply with group names only, "
                  f"comma separated, or 'none'.\n{listing}\n\nRequest: {query}")
        text = await self.llm.complete([{"role": "user", "content": prompt}])
        return [t.strip() for t in re.split(r"[,\s]+", text) if t.strip()]

    async def _select_tools(self, query: str) -> list[dict[str, Any]]:
        if not self.tools:
            self.tools_note = ""
            return []
        catalog = self.tools.servers_catalog()
        missing = [s for s in ("browser", "files", "shell") if s not in catalog]
        # Tell her honestly when the PC link is down, instead of letting her claim she has no internet.
        self.tools_note = ("Right now your link to the user's computer is down, so the browser, files and the terminal "
                           "are temporarily unavailable (web search, email, calendar, tasks and Seryn still work). If "
                           "asked for them, say the computer link is down; never say you have no internet access."
                           if missing else "")
        # Route on the previous user message too, so a follow-up ("and the other one?") keeps its tools.
        prev = [m["content"] for m in reversed(self.history.messages)
                if m.get("role") == "user" and isinstance(m.get("content"), str)][:2]
        query = " ".join([query, *(p[:300] for p in prev)])
        return await self.router.select(query, catalog, self._llm_pick_servers)

    # ------------------------------------------------------------------ tool execution

    async def _more_tools(self, need: str, tools: list[dict[str, Any]], known: set[str]) -> str:
        """Add the tools for `need` (a job description or a skill name) to this turn's list, mid-turn."""
        catalog = self.tools.servers_catalog() if self.tools else {}
        named = [n for n in catalog if n.replace("_", " ") in need.lower() or n in need.lower()]
        picked = [t for n in named for t in catalog[n]["tools"]]
        if not picked:
            scores = self.router._server_scores(need, catalog)
            best = [n for n, sc in sorted(scores.items(), key=lambda kv: -kv[1]) if sc > 0][:2]
            picked = [t for n in best for t in catalog[n]["tools"]]
        added = [t for t in picked if t["function"]["name"] not in known][:40]
        for t in added:
            tools.insert(len(tools) - 1, t)
            known.add(t["function"]["name"])
        if not added:
            have = ", ".join(sorted(catalog)) or "none"
            return (f"No more tools match '{need}'. Skills that exist: {have}. If the one you need isn't there (for "
                    "example the computer link is down), tell the user plainly.")
        return "Now available: " + ", ".join(t["function"]["name"] for t in added) + ". Use them."

    async def _execute(self, call: ToolCall, known: set[str], cb: TurnCallbacks) -> str:
        args = {k: v for k, v in call.arguments.items() if not k.startswith("_")}
        if call.name not in known or self.tools is None:
            return f"ERROR: there is no tool named {call.name}."
        decision = self.permissions.decide(call.name, args)
        confirmed: bool | None = None
        if decision.tier == "blocked":
            self.audit.record(tool=call.name, args=args, tier="blocked", confirmed=None, executed=False, error=decision.reason)
            cb.show("tool", f"[blocked] {call.name} {args}: {decision.reason}")
            return f"BLOCKED: this action is not allowed ({decision.reason}). Tell the user it is blocked; do not retry."
        sticky = getattr(self, "_approved", {})
        if decision.tier == "confirm" and sticky.get(call.name, 0) > time.time():
            confirmed = True                      # the user said yes to this kind of action a few minutes ago
            cb.show("tool", f"[already approved] {call.name}")
        elif decision.tier == "confirm":
            question = describe_call(call.name, args)
            cb.show("tool", f"[confirm?] {call.name} {json.dumps(args, default=str)[:300]}")
            reply = await cb.confirm(question)
            confirmed = is_affirmative(reply, self.cfg.permissions.confirm_words)
            if confirmed and call.name in STICKY:
                self._approved = {**sticky, call.name: time.time() + STICKY_S}
            if not confirmed:
                self.audit.record(tool=call.name, args=args, tier="confirm", confirmed=False, executed=False)
                return "DECLINED: the user did not confirm, so nothing was done. Acknowledge briefly; do not retry."
        if self.cfg.permissions.dry_run:
            self.audit.record(tool=call.name, args=args, tier=decision.tier, confirmed=confirmed, executed=False, dry_run=True)
            cb.show("tool", f"[dry-run] {call.name} {args}")
            return f"DRY RUN: {call.name} was not actually executed (dry-run mode). Tell the user what would have happened."

        cb.show("tool", f"-> {call.name} {json.dumps(args, default=str)[:300]}")
        start = time.perf_counter()

        async def filler():
            await asyncio.sleep(1.0)
            await cb.say(random.choice(FILLERS))

        filler_task = asyncio.create_task(filler()) if confirmed is None else None
        if self.store and call.name.endswith("__search_conversations"):
            await asyncio.to_thread(self.store.flush)   # so the newest messages are searchable
        try:
            result = await self.tools.call_tool(call.name, args, confirmed=bool(confirmed))
            error = ""
        except Exception as e:  # noqa: BLE001
            result, error = f"ERROR: {e}", str(e)
        finally:
            if filler_task:
                filler_task.cancel()
        duration = (time.perf_counter() - start) * 1000
        result = redact_secrets(result)
        self.audit.record(tool=call.name, args=args, tier=decision.tier, confirmed=confirmed, executed=not error,
                          result=result, error=error, duration_ms=duration)
        cb.show("tool", f"<- {call.name} ({duration:.0f} ms): {result[:200]}")
        if len(result) > MAX_TOOL_RESULT_CHARS:
            result = result[:MAX_TOOL_RESULT_CHARS] + "\n[truncated]"
        return result

    # ------------------------------------------------------------------ main entry

    async def handle(self, user_text: str, cb: TurnCallbacks) -> TurnResult:
        result = TurnResult()
        t0 = time.perf_counter()

        if RESET_RE.match(user_text):
            self.history.reset()
            self.summary, self.last_active = "", None
            if self.store:
                try:
                    self.store.reset(self.session_id)
                except Exception:  # noqa: BLE001
                    pass
            await cb.say("Okay, starting a new conversation.")
            return result
        if RELOAD_RE.match(user_text) and self.tools:
            problems = await self.tools.reload()
            n = sum(len(s["tools"]) for s in self.tools.servers_catalog().values())
            msg = f"Reloaded. I have {n} tools available."
            if problems:
                msg += " " + " ".join(problems)
            await cb.say(msg)
            return result

        tools = await self._select_tools(user_text)
        known = {t["function"]["name"] for t in tools}
        if self.tools and sum(len(s["tools"]) for s in self.tools.servers_catalog().values()) > len(tools):
            tools = [*tools, MORE_TOOLS]      # not every tool fits in one turn: let her fetch what's missing
        turn: list[dict[str, Any]] = [{"role": "user", "content": user_text}]

        self._spoke = False
        try:
            return await self._run(turn, tools, known, cb, result, t0)
        except asyncio.CancelledError:
            if not self._spoke:
                raise   # cancelled before she said a word (you were still talking): keep no half-request
            # Keep only the user's words: a half-finished tool exchange would be rejected by
            # OpenAI-style APIs on the next turn (every tool call needs its result).
            heard = ""
            try:
                heard = cb.heard() if cb.heard else ""
            except Exception:  # noqa: BLE001
                pass
            note = (f"{heard} [The user interrupted me here and did not hear the rest.]" if heard
                    else "(interrupted by the user)")
            self.history.extend([turn[0], {"role": "assistant", "content": note}])
            self._save(user_text, note)
            raise

    async def _run(self, turn, tools, known, cb, result, t0) -> TurnResult:
        first_token_seen = False
        say_to_user = cb.say

        async def say_unless_silent(sentence: str) -> None:
            if not _is_silent(sentence):
                self._spoke = True
                await say_to_user(sentence)

        cb = TurnCallbacks(say=say_unless_silent, confirm=cb.confirm, show=cb.show, on_first_token=cb.on_first_token,
                           heard=cb.heard)
        nudged = False          # retried once after an empty reply
        nudge: list[dict[str, Any]] = []
        for _iteration in range(self.cfg.llm.max_tool_iterations + 2):
            compact_turn(turn)
            messages = [self._system(), *self.history.messages, *turn, *nudge]
            chunker = SentenceChunker()
            full_text = ""
            spoken_upto_screen = True  # False once we hit the SCREEN: marker
            calls: list[ToolCall] = []
            offer_tools = tools if (_iteration < self.cfg.llm.max_tool_iterations and not nudged) else None
            finish, reasoning = "", ""
            async for ev in self.llm.chat_stream(messages, offer_tools):
                if ev.text:
                    if not first_token_seen:
                        first_token_seen = True
                        result.timings["first_token_ms"] = (time.perf_counter() - t0) * 1000
                        cb.on_first_token()
                    full_text += ev.text
                    if spoken_upto_screen:
                        m = SCREEN_MARKER.search(full_text)
                        piece = ev.text
                        if m:
                            spoken_upto_screen = False
                            # Only speak what came before the marker in this delta.
                            before_len = m.start() - (len(full_text) - len(ev.text))
                            piece = ev.text[:max(before_len, 0)]
                        for sentence in chunker.push(piece):
                            await cb.say(sentence)
                if ev.done:
                    calls = ev.tool_calls
                    finish = str((ev.stats or {}).get("finish_reason", ""))
                    reasoning = str((ev.stats or {}).get("reasoning_content", ""))
            tail = chunker.flush()
            if tail and not SCREEN_MARKER.search(tail):
                await cb.say(tail)
            m = SCREEN_MARKER.search(full_text)
            if m:
                cb.show("screen", full_text[m.end():].strip())

            if not calls and not full_text.strip():
                # The model came back empty (it happens after tool calls, or when it spends its token
                # budget thinking). Never leave the user in silence: ask once more without tools.
                import logging
                logging.getLogger("iriis.agent").warning("empty LLM reply (finish_reason=%r, nudged=%s)", finish, nudged)
                result.timings["empty_replies"] = result.timings.get("empty_replies", 0) + 1
                if not nudged:
                    nudged = True
                    nudge = [{"role": "user", "content": "(Your last reply was empty. Answer my request above now, "
                                                         "in plain spoken English. Don't call any tools.)"}]
                    continue
                full_text = "Sorry, I lost my train of thought there. Could you ask me that again?"
                await cb.say(full_text)

            if not calls and _is_silent(full_text):
                # Background speech that wasn't for her: say nothing, keep nothing.
                result.text = ""
                result.timings["ignored"] = 1
                result.timings["total_ms"] = (time.perf_counter() - t0) * 1000
                return result

            if not calls:
                spoken = SCREEN_MARKER.split(full_text)[0].strip()
                turn.append({"role": "assistant", "content": full_text})
                result.text = spoken
                break

            turn.append(LLMClient.assistant_tool_message(full_text, calls, reasoning))
            for call in calls:
                if call.name == MORE_TOOLS_NAME:
                    output = await self._more_tools(str(call.arguments.get("need", "")), tools, known)
                    turn.append(LLMClient.tool_result_message(call, output))
                    continue
                output = await self._execute(call, known, cb)
                result.tool_calls.append({"name": call.name, "arguments": call.arguments, "result": output[:300]})
                turn.append(LLMClient.tool_result_message(call, wrap_tool_result(call.name, output)))
        else:
            await cb.say("Sorry, I couldn't finish that within my step limit.")

        self.history.extend(turn)
        self._save(turn[0]["content"], result.text or "(no reply)")
        self.last_active = time.time()
        self._maybe_summarize()
        result.timings["total_ms"] = (time.perf_counter() - t0) * 1000
        return result
