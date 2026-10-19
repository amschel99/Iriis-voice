"""Her own mind (memory stream, lessons, goals, mood), the call watcher, call tools and the new studio tools."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from iriis.agent.mind import Mind, parse_json, run_calls
from iriis.storage import DB


class LLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def complete(self, messages, model=None):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else "{}"


def make_mind(tmp_path, replies=()):
    db = DB("", tmp_path / "m.sqlite3")
    said = []

    async def broadcast(t):
        said.append(("telegram", t))

    async def push(t):
        said.append(("push", t))

    async def send(m):
        said.append(("desktop", m))
    core = SimpleNamespace(db=db, llm=LLM(replies), cfg=SimpleNamespace(user_name="Amschel", assistant_name="Iriis",
                                                                       timezone="Africa/Nairobi"),
                           telegram=SimpleNamespace(broadcast=broadcast), push=push, desktop=SimpleNamespace(send=send),
                           conversations=SimpleNamespace(append=lambda *a: said.append(("chat", a))),
                           sessions=SimpleNamespace(sessions={}))
    mind = Mind(core)
    core.mind = mind
    return mind, core, said


def test_memory_stream_dedupes_and_retrieves_by_relevance(tmp_path):
    m, _, _ = make_mind(tmp_path)
    a = m.add("observation", "Amschel is testing the 12 C pack recipe in the lab this week", 6)
    assert m.add("observation", "Amschel is testing the 12 C pack recipe in the lab this week!", 8) == a   # reinforced
    m.add("observation", "Amschel prefers short spoken answers", 5)
    m.add("lesson", "When WhatsApp search times out, press Enter in the search box instead of clicking", 7)
    assert m.add("observation", "my password: hunter2", 9) is None                                          # never secrets
    top = m.retrieve("how is the pack recipe test going", ("observation",), 1)
    assert "recipe" in top[0][2] and top[0][3] == 8
    ctx = m.context("whatsapp message to Jane")
    assert "Lessons from your past mistakes" in ctx and "press Enter" in ctx and "Your goals" in ctx


def test_reflection_learns_facts_lessons_loops_and_mood(tmp_path):
    reply = json.dumps({"observations": [{"text": "Amschel's investor meeting is on Friday at 10am", "importance": 8}],
                        "people": [{"text": "Jane: Amschel's lab partner, on WhatsApp as Jane Lab", "importance": 7}],
                        "lesson": "Ask which file before making a PDF from 'that'", "skill": None,
                        "open_loops": ["Send Amschel the investor deck draft by Thursday"], "closed_loops": [],
                        "mood": {"feeling": "focused", "valence": 0.4, "why": "big week"}})
    m, _, _ = make_mind(tmp_path, [reply])
    asyncio.run(m._reflect("I have the investor meeting Friday 10am, Jane will help", "Got it.", []))
    kinds = {r[1] for r in m.rows(("observation", "person", "lesson", "loop"))}
    assert kinds == {"observation", "person", "lesson", "loop"}
    assert m.mood()["feeling"] == "focused"
    loop_id = m.loops()[0][0]
    m._apply_reflection({"closed_loops": [loop_id]})
    assert m.loops() == []


def test_trivial_turns_and_background_sessions_are_not_reflected(tmp_path):
    m, core, _ = make_mind(tmp_path)

    async def go():
        m.after_turn("owner", "hi", "Hey!", [])
        m.after_turn("task-3", "research freezers in Nairobi", "Done", [])
        await asyncio.sleep(0)
    asyncio.run(go())
    assert core.llm.prompts == []


def test_her_own_decisions_are_limited_and_never_contact_others(tmp_path):
    m, core, said = make_mind(tmp_path)

    async def go():
        assert await m.act({"action": "message", "message": "Your deck is due tomorrow."}) == "message"
        assert await m.act({"action": "call", "call_reason": "Investor meeting in 30 minutes"}) == "call"
        assert await m.act({"action": "task", "task": "Check flights to Kigali next week and list the cheapest three."}) == "task"
        await m.emergency({"to": "Jane", "message": "Please check on Amschel", "why": "he said he felt faint"}, "")
    asyncio.run(go())
    calls = core.db.execute("SELECT reason, source FROM calls ORDER BY id")
    assert calls[0] == ("Investor meeting in 30 minutes", "iriis") and len(calls) == 3
    assert core.db.execute("SELECT title FROM tasks")[0][0].startswith("(on my own)")
    last = [t for k, t in said if k == "push"][-1]
    assert "I'll send this to Jane" in last and "say yes" in last          # prepared, not sent
    for _ in range(10):
        asyncio.run(m.act({"action": "message", "message": "x" * 20}))
    assert m.today_count("mind_msgs") == 8                                 # daily limit


def test_parse_json_tolerates_thinking_and_prose():
    assert parse_json('<think>hmm {"a": 2}</think> Sure: {"action": "none"} ok') == {"action": "none"}
    assert parse_json("no json here") == {}


def test_calls_ring_repeat_and_missed_alarm_rings_again(tmp_path, monkeypatch):
    m, core, said = make_mind(tmp_path)
    pushed = []

    async def fake_push(db, title, body, url="/app", data=None):
        pushed.append((title, body, url, data))
        return 1
    import iriis.server.webapp as w
    monkeypatch.setattr(w, "push_all", fake_push)
    now = time.time()
    core.db.insert("INSERT INTO calls (at, reason, opening, status, repeat, source, created) VALUES (?, 'Wake up', '', "
                   "'scheduled', 'daily 06:30', 'user', ?)", (now - 1, now))

    async def go():
        t = asyncio.create_task(run_calls(core, every=0.05))
        await asyncio.sleep(0.3)
        core.db.execute("UPDATE calls SET rang = ? WHERE status = 'ringing'", (time.time() - 200,))   # nobody answered
        await asyncio.sleep(0.3)
        t.cancel()
    asyncio.run(go())
    assert pushed[0][3] == {"kind": "call", "call_id": 1} and pushed[0][2] == "/app?call=1"
    rows = core.db.execute("SELECT reason, status, repeat FROM calls ORDER BY id")
    assert rows[0][1] == "missed"
    assert ("Wake up", "scheduled", "daily 06:30") in rows                 # tomorrow's alarm
    assert ("Again: Wake up", "scheduled", "") in rows                     # snooze-like second try
    assert any(k == "telegram" and "calling" in t for k, t in said)


def test_schedule_call_tool(tmp_path, monkeypatch):
    import servers.tasks as T
    d = DB("", tmp_path / "t.sqlite3")
    monkeypatch.setattr(T, "get_db", lambda: d)
    out = T.schedule_call("now", "Time to leave for the lab")
    assert out.startswith("Call 1 set for now")
    out = T.schedule_call("06:30", "Wake up", repeat="daily 06:30")
    assert "repeating" in out
    assert "Wake up" in T.list_calls()
    assert "Cancelled call 2" in T.cancel_call(2)


@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    from servers import studio as S
    d = DB("", tmp_path / "s.sqlite3")
    monkeypatch.setattr(S, "get_db", lambda: d)
    return d


def test_plot_and_sketch(sdb):
    from servers import studio as S
    assert "Plot ready: file 1" in S.plot_math(functions=[{"expr": "tan(x)"}, {"expr": "x^2/10"}], x_range=[-5, 5])
    assert "Plot ready: file 2" in S.plot_math(parametric=[{"x": "cos(t)", "y": "sin(t)"}], hand_drawn=True, equal_aspect=True)
    out = S.make_sketch([{"shape": "rect", "x": 5, "y": 5, "w": 20, "h": 30, "label": "Fridge"},
                         {"shape": "arrow", "from": [25, 20], "to": [60, 20]}, {"shape": "text", "x": 50, "y": 60, "text": "Room"}])
    assert "Sketch ready: file 3" in out and S.load(3)[2][:4] == b"\x89PNG"
    with pytest.raises(ValueError, match="unknown shape"):
        S.make_sketch([{"shape": "dragon"}])


def test_safe_expr_refuses_code():
    from servers.studio import safe_expr
    assert float(safe_expr("2^3 + sqrt(16)")) == 12.0
    for bad in ("__import__('os')", "x.__class__", "open('f')", "[1][0]", "lambda: 1"):
        with pytest.raises((ValueError, SyntaxError)):
            safe_expr(bad, x=1)


def test_inspect_file_reads_documents(sdb):
    from servers import studio as S
    S.make_document("pdf", "Notes", "# Lab notes\nPack 12 held 14 hours.")
    assert "held 14 hours" in S.inspect_file(1)
    fid = S.store("data.csv", b"day,temp\nmon,12\n")
    assert "mon,12" in S.inspect_file(fid)


def test_memory_is_corrected_not_just_appended(tmp_path):
    m, _, _ = make_mind(tmp_path)
    a = m.add("observation", "Amschel's lab is in Ruaka", 6)
    b = m.add("observation", "Amschel drinks coffee at 10am daily", 4)
    m._apply_reflection({"updates": [{"id": a, "text": "Amschel's lab moved to Kilimani in October"}], "outdated": [b]})
    texts = [r[2] for r in m.rows(("observation",))]
    assert texts == ["Amschel's lab moved to Kilimani in October"]


def test_heartbeat_sees_how_her_reach_outs_landed_and_acts(tmp_path):
    decision = json.dumps({"thought": "Deck due tomorrow and no reply yet", "action": "message",
                           "message": "Quick nudge: the investor deck is due tomorrow.", "next_check_minutes": 90,
                           "mood": {"feeling": "attentive", "valence": 0.3, "why": "deadline"}})
    m, core, said = make_mind(tmp_path, [decision])
    m.add("observation", "I messaged Amschel on my own: lunch reminder", 3)
    m.set_autonomous(True)
    wait = asyncio.run(m.think())
    prompt = core.llm.prompts[0]
    assert "How your recent reach-outs landed" in prompt and "lunch reminder" in prompt and "no reply" in prompt
    assert wait == 90 * 60 and m.mood()["feeling"] == "attentive"
    assert ("push", "Quick nudge: the investor deck is due tomorrow.") in said
