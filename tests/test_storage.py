"""Storage tests, run against SQLite always and Postgres when IRIIS_TEST_PG_URL is set
(e.g. IRIIS_TEST_PG_URL=postgresql://postgres:@127.0.0.1:5432/postgres)."""
import json
import os
import threading
import time
import uuid

import pytest

from iriis.agent.longterm import LongTermMemory
from iriis.agent.permissions import AuditLog
from iriis.agent.reminders import ReminderWatcher
from iriis.storage import DB

PG_URL = os.environ.get("IRIIS_TEST_PG_URL", "")


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    if request.param == "sqlite":
        return DB("", tmp_path / "t.sqlite3")
    if not PG_URL:
        pytest.skip("set IRIIS_TEST_PG_URL to run the Postgres variant")
    d = DB(PG_URL)
    for t in ("facts", "notes", "reminders", "audit_log"):
        d.execute(f"DELETE FROM {t}")
    return d


def test_schema_is_idempotent(db):
    DB(db.url, db.path)  # second init must not fail
    assert db.execute("SELECT COUNT(*) FROM facts")[0][0] == 0


def test_memory_save_search_forget(db):
    mem = LongTermMemory(db)
    a = mem.save("My gym is at Sarit Centre in Westlands")
    b = mem.save("Wanjiru's birthday is on 3 March")
    assert a != b
    assert mem.search("where is my gym")[0][1].startswith("My gym")
    assert mem.forget("gym") == ["My gym is at Sarit Centre in Westlands"]
    assert [t for _, t in mem.all()] == ["Wanjiru's birthday is on 3 March"]
    assert mem.forget(str(b)) and mem.all() == []
    assert mem.forget("nothing like this") == []


def test_reminders_are_claimed_once(db):
    now = time.time()
    db.insert("INSERT INTO reminders (text, due, kind, created) VALUES (?, ?, 'timer', ?)", ("tea", now - 1, now))
    db.insert("INSERT INTO reminders (text, due, kind, created) VALUES (?, ?, 'reminder', ?)", ("later", now + 3600, now))

    async def nop(text):
        pass

    watcher = ReminderWatcher(nop, db)
    got: list = []
    threads = [threading.Thread(target=lambda: got.extend(watcher.due())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [(r[1], r[2]) for r in got] == [("tea", "timer")]   # exactly one claim, never the future one
    assert watcher.due() == []


def test_audit_log_goes_to_db(db, tmp_path):
    log = AuditLog(str(tmp_path / "a.jsonl"), db=db, source="cloud")
    log.record(tool="files__read_text_file", args={"path": "x", "api_key": "sk-secret"}, tier="auto",
               confirmed=None, executed=True, result="ok")
    rows = db.execute("SELECT source, tool, tier, executed, entry FROM audit_log")
    assert rows[0][:4] == ("cloud", "files__read_text_file", "auto", 1)
    assert "sk-secret" not in rows[0][4] and json.loads(rows[0][4])["args"]["api_key"] == "[REDACTED]"


def test_notes_server_tools_use_storage(db, monkeypatch):
    import iriis.storage as storage
    import servers.notes_reminders as notes
    monkeypatch.setattr(notes, "get_db", lambda: db)
    tag = uuid.uuid4().hex[:6]
    assert "Saved note" in notes.add_note(f"buy unga {tag}", title="Shopping")
    assert tag in notes.list_notes(tag.upper())          # case-insensitive search on both engines
    nid = int(notes.list_notes(tag).split()[0][1:])
    assert notes.delete_note(nid) == f"Deleted note #{nid}."
    assert notes.delete_note(nid) == f"No note #{nid}."
    out = notes.set_timer(0.5, "chai")
    rid = int(out.split("#")[1].split()[0])
    assert "chai" in notes.list_reminders()
    assert notes.cancel_reminder(rid) == f"Cancelled #{rid}."
    assert notes.list_reminders() == "No upcoming reminders or timers."
