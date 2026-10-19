"""Her agenda (rhythm, projects, hobbies, work alone with safe tools), her self-authored character, training data."""
from __future__ import annotations

import asyncio
import datetime as dt
import json

from iriis.agent.agenda import WORK_SERVERS, WorkTools, _sections, training_records
from tests.test_mind import make_mind


def test_rest_hours(tmp_path):
    m, _, _ = make_mind(tmp_path)
    m.set_kv("mind_rhythm", {"wake": "06:30", "sleep": "23:30"})
    tz = m.tz
    assert m.agenda.resting(dt.datetime(2026, 10, 20, 2, 0, tzinfo=tz))
    assert m.agenda.resting(dt.datetime(2026, 10, 20, 23, 45, tzinfo=tz))
    assert not m.agenda.resting(dt.datetime(2026, 10, 20, 9, 0, tzinfo=tz))


def test_work_tools_only_research_and_drafting():
    class Inner:
        def servers_catalog(self):
            return {k: {"tools": []} for k in ("web", "browser", "shell", "whatsapp", "studio", "hq", "money", "tasks")}

        async def call_tool(self, name, args, confirmed=False):
            return "ran " + name
    w = WorkTools(Inner())
    assert set(w.servers_catalog()) == {"web", "studio", "hq"}
    assert asyncio.run(w.call_tool("whatsapp__send_message", {})).startswith("BLOCKED")
    assert asyncio.run(w.call_tool("web__web_search", {})) == "ran web__web_search"
    assert not WORK_SERVERS & {"browser", "shell", "whatsapp", "files", "money", "tasks", "apps_and_media"}


def test_practice_keeps_the_lesson_and_shares_once(tmp_path):
    reply = json.dumps({"attempt": "a", "critique": ["x"], "revised": "A sharper pitch for Seryn.", "lesson":
                        "Lead with the customer's loss, then the fix", "worth_sharing": True})
    m, core, said = make_mind(tmp_path, [reply, reply])
    hid = m.agenda.hobbies()[0][0]
    asyncio.run(m.act({"action": "practice", "hobby": hid, "exercise": "Write a 60-word pitch for Seryn Cool"}))
    assert any("Lead with the customer's loss" in r[2] for r in m.rows(("skill",)))
    assert any(k == "push" and "practising" in t for k, t in said)
    n = len(said)
    asyncio.run(m.act({"action": "practice", "hobby": hid, "exercise": "Again"}))
    assert len([1 for k, t in said[n:] if k == "push"]) == 0          # shares at most once a day


def test_sections_parse_work_reports():
    out = _sections("FINDINGS: Three grants fit Seryn.\nNEXT: draft the EIC application outline\nSHARE: Found 3 grants worth 2M KES.")
    assert out == {"findings": "Three grants fit Seryn.", "next": "draft the EIC application outline",
                   "share": "Found 3 grants worth 2M KES."}
    assert _sections("FINDINGS: x\nSHARE: nothing yet")["share"] == ""


def test_she_authors_her_own_character_and_it_shapes_her_context(tmp_path):
    persona = {"purpose": "Make Amschel's ambitions real", "aesthetic": "Nairobi-futurist, calm and sharp",
               "voice": "short, warm, dry wit", "vocabulary": ["signal", "let's ship"], "address": "Amschel; 'boss' when teasing",
               "rituals": ["mornings start with the one thing that matters"], "avoid": ["corporate filler"]}
    m, core, _ = make_mind(tmp_path, [json.dumps(persona)])
    asyncio.run(m.author_persona())
    assert "author your own character" in core.llm.prompts[0].lower()
    ctx = m.context("hello")
    assert "Your character (you authored it" in ctx and "Nairobi-futurist" in ctx and "let's ship" in ctx


def test_training_records_mark_corrections(tmp_path):
    from iriis.storage import DB
    db = DB("", tmp_path / "t.sqlite3")
    for sid, role, text in [("owner", "user", "book the 9am"), ("owner", "assistant", "Booked 10am."),
                            ("owner", "user", "no, I said 9am"), ("owner", "assistant", "Fixed: 9am."),
                            ("task-3", "user", "research"), ("task-3", "assistant", "report")]:
        db.execute("INSERT INTO messages (session_id, role, content, created) VALUES (?, ?, ?, 1)", (sid, role, text))
    recs = training_records(db)
    assert len(recs) == 2 and recs[0]["corrected"] is True and recs[1]["corrected"] is False
    assert recs[0]["messages"][1]["content"] == "Booked 10am."


def test_finished_work_becomes_playbook_rules_in_her_prompt(tmp_path):
    """ACE-style: the writing research turns into rules that are injected when she writes, not for other topics."""
    rules = json.dumps({"rules": [
        {"domain": "writing", "rule": "Lead with the one true point in the first sentence.", "importance": 8},
        {"domain": "writing", "rule": "Cut every word that isn't doing work; prefer short Anglo-Saxon words.", "importance": 7},
        {"domain": "research", "rule": "Verify every quote against the original source before using it.", "importance": 7},
        {"domain": "writing", "rule": "too vague"}]})
    m, core, _ = make_mind(tmp_path, [rules])
    m._force = True
    n = asyncio.run(m.distill("background task: learn article writing", "Orwell, Zinsser, McPhee... six habits"))
    assert n == 3                                                        # the vague one was dropped
    ctx = m.context("help me write a post about Seryn")
    assert "Your playbook" in ctx and "Lead with the one true point" in ctx
    assert "Your playbook" not in m.context("what's the KPLC tariff per kWh")
    # sharpening an old rule replaces it instead of piling up
    rid = [r[0] for r in m.rows(("rule",)) if "Lead with" in r[2]][0]
    core.llm.replies.append(json.dumps({"rules": [{"domain": "writing", "rule": "Lead with the point; one claim per paragraph.",
                                                    "replaces": rid}]}))
    asyncio.run(m.distill("practice", "x"))
    texts = [r[2] for r in m.rows(("rule",))]
    assert not any("Lead with the one true point" in t for t in texts) and any("one claim per paragraph" in t for t in texts)


def test_task_runner_learns_only_from_finished_tasks(tmp_path, monkeypatch):
    from iriis.agent.tasks import TaskRunner
    from iriis.storage import DB
    import servers.tasks as T
    db = DB("", tmp_path / "t.sqlite3")
    monkeypatch.setattr(T, "get_db", lambda: db)
    T.start_task("Learn article writing from credible sources and write a guide", title="Writing")
    T.start_task("Compare three freezer suppliers in Nairobi", title="Freezers")
    learned = []
    replies = {1: "Notes so far.\nSTATUS: continue", 2: "Here is the comparison.\nSTATUS: done"}

    async def run(tid, prompt):
        return replies[tid]

    async def notify(spoken, full):
        pass
    r = TaskRunner(run, notify, db, learn=lambda goal, report: learned.append(goal))

    async def go():
        for row in r.due(5):
            await r._work(row)
    asyncio.run(go())
    assert learned == ["Compare three freezer suppliers in Nairobi"]
