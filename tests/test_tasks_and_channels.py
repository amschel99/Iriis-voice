"""Background tasks + routines, the task runner, Telegram message splitting, Google helpers."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt

import pytest

from servers import tasks as T


@pytest.fixture()
def tdb(tmp_path, monkeypatch):
    from iriis.storage import DB
    d = DB("", tmp_path / "t.sqlite3")
    monkeypatch.setattr(T, "get_db", lambda: d)
    return d


def test_parse_repeat_forms():
    assert T.parse_repeat("daily 7am") == "daily 07:00"
    assert T.parse_repeat("every weekday at 7:30") == "weekdays 07:30"
    assert T.parse_repeat("every monday 9am") == "monday 09:00"
    assert T.parse_repeat("every 3 hours") == "every 10800s"
    assert T.parse_repeat("") == ""
    with pytest.raises(ValueError):
        T.parse_repeat("every 5 minutes")
    with pytest.raises(ValueError):
        T.parse_repeat("whenever")


def test_next_run_respects_days():
    fri = dt.datetime(2026, 10, 23, 8, 0, tzinfo=T.TZ).timestamp()        # Friday 08:00
    nxt = dt.datetime.fromtimestamp(T.next_run("weekdays 07:00", fri), T.TZ)
    assert nxt.weekday() == 0 and nxt.hour == 7                              # Monday, not Saturday
    nxt = dt.datetime.fromtimestamp(T.next_run("daily 09:00", fri), T.TZ)
    assert nxt.date() == dt.date(2026, 10, 23) and nxt.hour == 9
    assert T.next_run("every 3600s", fri) == fri + 3600
    assert T.next_run("", fri) is None


def test_task_lifecycle(tdb):
    out = T.start_task("Compare three chest freezer prices in Nairobi with warranties")
    assert "starts now" in out
    T.start_task("Morning briefing: weather, calendar and unread email", repeat="daily 07:00")
    listing = T.list_tasks()
    assert "Morning briefing" in listing and "daily at 07:00" in listing
    assert "Cancelled" in T.cancel_task(2)
    assert "cancelled" in T.list_tasks("cancelled")
    with pytest.raises(ValueError):
        T.start_task("hi")


def test_runner_runs_due_tasks_and_reschedules_routines(tdb):
    from iriis.agent.tasks import TaskRunner
    T.start_task("Find the KPLC tariff for small businesses", title="KPLC tariff")
    T.start_task("Check the unread email and summarise", repeat="every 2 hours", title="Inbox")
    seen, told = [], []

    async def run(tid, prompt):
        seen.append(prompt)
        return f"Report {tid}. More detail."

    async def notify(spoken, full):
        told.append(spoken)

    r = TaskRunner(run, notify, tdb, interval_s=0.01)

    async def go():
        for row in r.due(5):
            await r._work(row)

    asyncio.run(go())
    assert len(seen) == 2 and "NOT listening" in seen[0] and "KPLC tariff" in seen[0]
    rows = dict((tid, (st, res)) for tid, st, res in tdb.execute("SELECT id, status, result FROM tasks"))
    assert rows[1][0] == "done" and rows[1][1].startswith("Report 1")
    assert rows[2][0] == "scheduled"                                       # a routine goes round again
    assert any("KPLC tariff is done. Report 1." in t for t in told)
    assert r.due(5) == []                                                  # nothing due twice


def test_runner_recovers_interrupted_tasks(tdb):
    from iriis.agent.tasks import TaskRunner
    T.start_task("Research suppliers of HDPE bottles in Kenya")
    tdb.execute("UPDATE tasks SET status = 'running'")
    r = TaskRunner(None, None, tdb)
    assert r.recover() == 1 and len(r.due(5)) == 1


def test_telegram_split_message():
    from iriis.server.telegram import split_message
    text = ("para one. " * 300) + "\n\n" + ("para two. " * 300)
    parts = split_message(text, 4000)
    assert all(len(p) <= 4000 for p in parts) and "".join(parts).replace(" ", "") == text.replace(" ", "").replace("\n", "")


def test_telegram_only_serves_allowed_chats():
    from iriis.server.telegram import TelegramBridge
    sent = []

    class B(TelegramBridge):
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

    b = B(core=None, token="x", chat_ids="111")
    asyncio.run(b.handle({"message": {"chat": {"id": 999}, "text": "show my email"}}))
    assert sent == [(999, "This assistant is private.")]
    b2 = B(core=None, token="x", chat_ids="")
    asyncio.run(b2.handle({"message": {"chat": {"id": 5}, "text": "hi"}}))
    assert "TELEGRAM_CHAT_ID=5" in sent[-1][1]


def test_google_body_text_and_dates():
    from servers import google as G
    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": enc("Hi Amschel,\nThe quote is KES 45,000.\n\nOn Mon, 19 Oct 2026 Jane wrote:\n> old stuff")}},
        {"mimeType": "text/html", "body": {"data": enc("<p>ignored</p>")}}]}
    t = G.body_text(payload)
    assert "KES 45,000" in t and "old stuff" not in t
    now = dt.datetime(2026, 10, 19, 10, 0, tzinfo=G.TZ)               # Monday
    assert G.parse_dt("tomorrow 9am", now) == dt.datetime(2026, 10, 20, 9, 0, tzinfo=G.TZ)
    assert G.parse_dt("friday 15:30", now).day == 23
    assert G.parse_dt("monday 9am", now).day == 26                     # next Monday, not today
    assert G.parse_dt("2026-11-01 14:00", now).hour == 14


def test_google_not_connected_is_explained(monkeypatch, tmp_path):
    from servers import google as G
    for k in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
        monkeypatch.delenv(f"GOOGLE_{k}", raising=False)
    monkeypatch.setenv("IRIIS_SQLITE_PATH", str(tmp_path / "x.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert "iriis google-login" in G.google_status()


def test_overwriting_a_document_asks_first():
    from iriis.agent.permissions import PermissionManager
    from iriis.config import Config, load_mcp_servers
    pm = PermissionManager(Config(), load_mcp_servers())
    assert pm.decide("documents__create_document", {"filename": "a.docx"}).tier == "auto"
    assert pm.decide("documents__create_document", {"filename": "a.docx", "overwrite": True}).tier == "confirm"
    assert pm.decide("google__gmail_send_draft", {}).tier == "confirm"
    assert pm.decide("google__gmail_draft", {}).tier == "auto"
