"""Persistent conversations: saved as you talk, reloaded after restarts, summarised when long."""
import asyncio
import time

from iriis.agent.conversations import ConversationStore
from iriis.agent.loop import Agent, TurnCallbacks
from iriis.agent.permissions import PermissionManager
from iriis.config import Config
from iriis.llm.base import LLMClient, LLMEvent


class ScriptLLM(LLMClient):
    """Replies with a fixed text; records every request it receives."""

    def __init__(self, reply="Okay.", summary="SUMMARY: user is planning a trip to Mombasa."):
        self.model = "fake"
        self.reply, self.summary_text = reply, summary
        self.requests: list[list[dict]] = []

    async def chat_stream(self, messages, tools=None, model=None):
        self.requests.append(messages)
        is_summary = "maintain" in messages[-1]["content"] and "long-term memory" in messages[-1]["content"]
        yield LLMEvent(text=self.summary_text if is_summary else self.reply)
        yield LLMEvent(done=True)

    async def health(self):
        return True, "ok"


def make_agent(db, tmp_path, llm=None, max_tokens=5000):
    cfg = Config()
    cfg.llm.history_max_tokens = max_tokens
    cfg.permissions.audit_log = str(tmp_path / "audit.jsonl")
    agent = Agent(cfg, llm or ScriptLLM(), None, PermissionManager(cfg, []), store=ConversationStore(db))
    return agent


def say(agent, text):
    async def nop(*_):
        return ""
    async def run():
        r = await agent.handle(text, TurnCallbacks(say=nop, confirm=nop))
        if agent._summarizing:
            await agent._summarizing
        return r
    return asyncio.run(run())


def test_conversation_survives_a_restart(db, tmp_path):
    a = make_agent(db, tmp_path)
    say(a, "I'm planning a trip to Mombasa next week")
    say(a, "I want to take the SGR train")
    # "Restart": a brand-new agent on the same database
    llm = ScriptLLM()
    b = make_agent(db, tmp_path, llm)
    say(b, "how should I get there?")
    sent = [m["content"] for m in llm.requests[0] if m["role"] == "user"]
    assert sent == ["I'm planning a trip to Mombasa next week", "I want to take the SGR train", "how should I get there?"]


def test_start_over_resets_context_but_keeps_archive(db, tmp_path):
    a = make_agent(db, tmp_path)
    say(a, "my sister Akinyi lives in Kisumu")
    say(a, "start over")
    llm = ScriptLLM()
    b = make_agent(db, tmp_path, llm)
    say(b, "hello")
    assert [m["content"] for m in llm.requests[0] if m["role"] == "user"] == ["hello"]
    hits = ConversationStore(db).search("Akinyi")
    assert hits and "Kisumu" in hits[0][3]


def test_old_turns_are_summarised_and_injected(db, tmp_path):
    a = make_agent(db, tmp_path, max_tokens=60)   # tiny window forces trimming
    for i in range(6):
        say(a, f"message number {i} about the Mombasa trip and the SGR train tickets")
    store = ConversationStore(db)
    assert "Mombasa" in store.summary("owner")
    llm = ScriptLLM()
    b = make_agent(db, tmp_path, llm, max_tokens=60)
    say(b, "remind me what we planned")
    system = llm.requests[0][0]["content"]
    assert "Summary of the earlier parts" in system and "Mombasa" in system


def test_long_gap_is_flagged_as_a_new_sitting(db, tmp_path):
    store = ConversationStore(db)
    store.append("owner", "user", "let's talk tomorrow")
    store.append("owner", "assistant", "Sure.")
    db.execute("UPDATE messages SET created = ?", (time.time() - 26 * 3600,))
    llm = ScriptLLM()
    b = make_agent(db, tmp_path, llm)
    say(b, "hi again")
    assert "new sitting" in llm.requests[0][0]["content"]


def test_search_and_purge(db, tmp_path):
    store = ConversationStore(db)
    store.append("owner", "user", "the plumber is coming on Thursday")
    store.append("other-app", "user", "unrelated")
    assert len(store.search("plumber")) == 1
    assert store.search("plumber", session_id="other-app") == []
    store.purge("owner")
    assert store.search("plumber") == []


def test_search_conversations_tool(db, monkeypatch):
    import servers.memory as mem
    monkeypatch.setattr(mem, "conversations", ConversationStore(db))
    mem.conversations.append("owner", "user", "my landlord's name is Mr Kamau")
    mem.conversations.append("owner", "assistant", "Got it, Mr Kamau.")
    out = mem.search_conversations("landlord")
    assert "user: my landlord's name is Mr Kamau" in out
    assert "No past conversation" in mem.search_conversations("zebra")


def test_write_behind_buffers_until_flush(db):
    store = ConversationStore(db, write_behind=True)
    store.append("owner", "user", "buffered hello")
    store.append("owner", "assistant", "buffered reply")
    assert store.pending == 2
    assert db.execute("SELECT COUNT(*) FROM messages")[0][0] == 0      # nothing written yet
    assert store.flush() == 2 and store.pending == 0
    assert db.execute("SELECT COUNT(*) FROM messages")[0][0] == 2


def test_reads_flush_first_so_nothing_is_missed(db):
    store = ConversationStore(db, write_behind=True)
    store.append("owner", "user", "the matatu fare went up")
    assert store.search("matatu")                                       # search flushes
    store.append("owner", "assistant", "Noted.")
    assert [m["content"] for m in store.recent("owner", 1000)] == ["the matatu fare went up", "Noted."]


def test_failed_flush_keeps_messages_for_retry(db, monkeypatch):
    store = ConversationStore(db, write_behind=True)
    store.append("owner", "user", "keep me")
    real = db.executemany
    monkeypatch.setattr(db, "executemany", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert store.flush() == 0 and store.pending == 1
    monkeypatch.setattr(db, "executemany", real)
    assert store.flush() == 1


def test_flusher_runs_in_background(db):
    store = ConversationStore(db, write_behind=True)
    store.append("owner", "user", "tick")

    async def run():
        task = asyncio.create_task(store.run_flusher(0.05))
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(run())
    assert store.pending == 0 and db.execute("SELECT COUNT(*) FROM messages")[0][0] == 1


def test_system_prompt_always_renders():
    """A stray { } in the prompt template would crash every request (str.format)."""
    from iriis.llm.prompts import build_system_prompt
    p = build_system_prompt("Iriis", "Amschel", "Nairobi, Kenya", "Africa/Nairobi", ["gym at Sarit"],
                            conversation_summary="planning a Mombasa trip", last_active=time.time() - 7200)
    assert "You are Iriis" in p and "{hannah}" in p and "gym at Sarit" in p and "new sitting" in p


class SequenceLLM(LLMClient):
    """Plays a list of replies in order; "" = an empty reply. Records whether tools were offered."""
    def __init__(self, replies):
        self.model, self.replies, self.offered = "fake", list(replies), []

    async def chat_stream(self, messages, tools=None, model=None):
        self.offered.append(bool(tools))
        r = self.replies.pop(0) if self.replies else ""
        if r:
            yield LLMEvent(text=r)
        yield LLMEvent(done=True, stats={"finish_reason": "stop"})

    async def health(self):
        return True, "ok"


def _say_and_collect(agent, text):
    spoken = []
    async def say(s):
        spoken.append(s)
    async def nop(*_):
        return ""
    r = asyncio.run(agent.handle(text, TurnCallbacks(say=say, confirm=nop)))
    return r, spoken


def test_empty_reply_is_retried_without_tools(db, tmp_path):
    llm = SequenceLLM(["", "Phase change materials absorb heat as they melt."])
    a = make_agent(db, tmp_path, llm)
    r, spoken = _say_and_collect(a, "how do phase change materials work?")
    assert r.text.startswith("Phase change materials") and spoken
    assert llm.offered[1] is False                      # the retry offers no tools
    assert "empty" not in " ".join(m["content"] for m in a.history.messages if m["role"] == "user")  # nudge not saved


def test_never_silent_even_if_retry_is_empty(db, tmp_path):
    a = make_agent(db, tmp_path, SequenceLLM(["", ""]))
    r, spoken = _say_and_collect(a, "hello?")
    assert "ask me that again" in r.text and spoken


def test_background_speech_is_ignored_silently(db, tmp_path):
    a = make_agent(db, tmp_path, SequenceLLM(["(silent)"]))
    r, spoken = _say_and_collect(a, "and in other news the shilling rose today")   # a TV in the background
    assert spoken == [] and r.text == "" and r.timings.get("ignored") == 1
    assert a.history.messages == []                                                 # nothing kept


def test_request_cancelled_before_she_speaks_leaves_no_trace(db, tmp_path):
    """You paused, then kept talking: the half request is dropped and the joined one replaces it."""
    class SlowLLM(SequenceLLM):
        async def chat_stream(self, messages, tools=None, model=None):
            await asyncio.sleep(5)
            yield LLMEvent(done=True)

    a = make_agent(db, tmp_path, SlowLLM([]))

    async def run():
        async def nop(*_):
            return ""
        task = asyncio.create_task(a.handle("I want you to explain to me", TurnCallbacks(say=nop, confirm=nop)))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    assert a.history.messages == []
    assert ConversationStore(db).search("explain") == []


def test_interruption_remembers_what_the_user_heard(tmp_path):
    """You cut her off during sentence 2: her memory keeps sentence 1 + where she was cut off."""
    import numpy as np
    from iriis.agent.loop import Agent
    from iriis.agent.permissions import PermissionManager
    from iriis.config import Config
    from iriis.server.sessions import Session, run_turn

    class ThreeSentences(LLMClient):
        model = "fake"
        async def chat_stream(self, messages, tools=None, model=None):
            for t in ["The first option is paraffin. ", "The second option is a salt hydrate. ", "The third is water gel."]:
                yield LLMEvent(text=t)
            yield LLMEvent(done=True)
        async def health(self):
            return True, ""

    class SlowSpeech:
        async def stream(self, text, voice=None):
            for _ in range(2):
                await asyncio.sleep(0.05)
                yield np.zeros(4, dtype=np.float32), 24000

    cfg = Config()
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    agent = Agent(cfg, ThreeSentences(), None, PermissionManager(cfg, []))
    session = Session("s", agent)
    seqs = []

    async def emit(ev, data):
        if ev["type"] == "audio":
            seqs.append(ev["seq"])

    async def go():
        task = asyncio.create_task(run_turn(session, "what are my options", emit, speech=SlowSpeech(), speak=True))
        while len(seqs) < 3:                 # sentence 1 (2 chunks) + first chunk of sentence 2 heard
            await asyncio.sleep(0.01)
        session.heard_seq = 3
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    said = agent.history.messages[-1]["content"]
    assert said.startswith("The first option is paraffin.")
    assert "cut off" in said and "interrupted me here" in said and "third" not in said


def test_one_huge_turn_is_trimmed_without_looping_forever():
    """A background task's single turn with many long pages used to spin trim() forever (froze the brain)."""
    from iriis.agent.memory import ConversationHistory
    h = ConversationHistory(max_tokens=2000)
    h.add({"role": "user", "content": "research KPLC tariffs"})
    for i in range(12):
        h.add({"role": "assistant", "content": "", "tool_calls": []})
        h.add({"role": "tool", "content": "page text " * 2000, "tool_call_id": str(i)})
    assert all(len(m["content"]) <= 2012 for m in h.messages if m["role"] == "tool")


def test_long_turns_shorten_older_tool_results():
    from iriis.agent.loop import compact_turn
    turn = [{"role": "user", "content": "research"}]
    for i in range(10):
        turn.append({"role": "tool", "content": f"page {i} " + "x" * 10_000})
    compact_turn(turn, budget=40_000, keep=3)
    total = sum(len(m["content"]) for m in turn)
    assert total <= 3 * 10_010 + 7 * 1_530      # 3 newest whole + 7 heads (from ~100k)
    assert all(len(m["content"]) > 10_000 for m in turn[-3:])          # the newest stay whole
    assert turn[1]["content"].endswith("[shortened to save space]") and turn[1]["content"].startswith("page 0")
    assert compact_turn(turn, budget=40_000, keep=3) == 0               # idempotent
