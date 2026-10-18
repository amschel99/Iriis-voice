"""notes_reminders MCP server: local notes, reminders and timers in SQLite.

The server only stores reminders. The Iriis main process polls the same database
(iriis.agent.reminders.ReminderWatcher) and speaks them when they fall due.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from servers._common import data_dir, make_server

mcp = make_server("notes_reminders")
DB = data_dir() / "notes.sqlite3"
TZ = ZoneInfo("Africa/Nairobi")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due REAL NOT NULL,
  kind TEXT NOT NULL DEFAULT 'reminder', fired INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
"""


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=5)
    c.executescript(SCHEMA)
    return c


def _fmt(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts, TZ)
    today = dt.datetime.now(TZ).date()
    day = "today" if d.date() == today else "tomorrow" if d.date() == today + dt.timedelta(days=1) else d.strftime("%A %d %B")
    return f"{day} at {d.strftime('%H:%M')}"


def parse_when(when: str, now: dt.datetime | None = None) -> dt.datetime:
    """Accepts 'in 10 minutes', 'in 2 hours', '17:30', '5pm', 'tomorrow 8am', '2026-10-20 09:00'."""
    now = now or dt.datetime.now(TZ)
    s = when.strip().lower()
    m = re.fullmatch(r"in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)", s)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        secs = n * (1 if unit.startswith("s") else 60 if unit.startswith("m") else 3600 if unit.startswith("h") else 86400)
        return now + dt.timedelta(seconds=secs)
    try:
        return dt.datetime.fromisoformat(when.strip()).replace(tzinfo=TZ)
    except ValueError:
        pass
    day_offset = 0
    if s.startswith("tomorrow"):
        day_offset, s = 1, s[len("tomorrow"):].strip(" at")
    elif s.startswith("today"):
        s = s[len("today"):].strip(" at")
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)?", s)
    if not m:
        raise ValueError(f"Could not understand the time '{when}'. Try 'in 10 minutes' or '17:30'.")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3).startswith("p") else 0)
    target = now.replace(hour=h, minute=mi, second=0, microsecond=0) + dt.timedelta(days=day_offset)
    if target <= now and day_offset == 0:
        target += dt.timedelta(days=1)
    return target


@mcp.tool()
def add_note(text: str, title: str = "") -> str:
    """Save a note."""
    with _db() as c:
        nid = c.execute("INSERT INTO notes(title, body, created) VALUES (?, ?, ?)", (title, text, time.time())).lastrowid
    return f"Saved note #{nid}."


@mcp.tool()
def list_notes(search: str = "", limit: int = 10) -> str:
    """List recent notes, optionally filtered by a search word."""
    with _db() as c:
        rows = list(c.execute(
            "SELECT id, title, body, created FROM notes WHERE body LIKE ? OR title LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{search}%", f"%{search}%", max(1, min(limit, 50)))))
    if not rows:
        return "No notes found."
    return "\n".join(f"#{i} ({_fmt(cr)}){' ' + t + ':' if t else ''} {b}" for i, t, b, cr in rows)


@mcp.tool()
def delete_note(note_id: int) -> str:
    """Delete a note by its number."""
    with _db() as c:
        n = c.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount
    return f"Deleted note #{note_id}." if n else f"No note #{note_id}."


@mcp.tool()
def set_reminder(text: str, when: str) -> str:
    """Set a reminder. when: 'in 20 minutes', '17:30', '5pm', 'tomorrow 8am' or 'YYYY-MM-DD HH:MM'."""
    due = parse_when(when)
    with _db() as c:
        rid = c.execute("INSERT INTO reminders(text, due, kind, created) VALUES (?, ?, 'reminder', ?)",
                        (text, due.timestamp(), time.time())).lastrowid
    return f"Reminder #{rid} set for {_fmt(due.timestamp())}: {text}"


@mcp.tool()
def set_timer(minutes: float, label: str = "timer") -> str:
    """Start a countdown timer for a number of minutes (fractions allowed)."""
    due = time.time() + minutes * 60
    with _db() as c:
        rid = c.execute("INSERT INTO reminders(text, due, kind, created) VALUES (?, ?, 'timer', ?)",
                        (label, due, time.time())).lastrowid
    return f"Timer #{rid} set for {minutes:g} minutes, ending {_fmt(due)}."


@mcp.tool()
def list_reminders() -> str:
    """List upcoming reminders and timers."""
    with _db() as c:
        rows = list(c.execute("SELECT id, text, due, kind FROM reminders WHERE fired = 0 ORDER BY due"))
    if not rows:
        return "No upcoming reminders or timers."
    return "\n".join(f"#{i} {k} {_fmt(d)}: {t}" for i, t, d, k in rows)


@mcp.tool()
def cancel_reminder(reminder_id: int) -> str:
    """Cancel a reminder or timer by its number."""
    with _db() as c:
        n = c.execute("DELETE FROM reminders WHERE id = ? AND fired = 0", (reminder_id,)).rowcount
    return f"Cancelled #{reminder_id}." if n else f"No pending reminder #{reminder_id}."


if __name__ == "__main__":
    mcp.run()
