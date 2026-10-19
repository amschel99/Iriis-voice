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
