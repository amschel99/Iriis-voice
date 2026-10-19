"""tasks MCP server: background jobs and routines Iriis works on by herself (Postgres on Railway, SQLite locally).

A task is a goal written as an instruction to Iriis ("compare three freezer suppliers in Nairobi and summarise
prices and warranties"). It runs now, at a set time, or on a schedule (a routine: "every weekday 07:00 morning
briefing"). The server only stores tasks; the Iriis cloud core (iriis.agent.tasks.TaskRunner) claims due tasks,
works through them with her tools in a separate session, saves the report and tells the user (voice, Telegram).

Background tasks can't get the user's confirmation, so anything that needs it (sending, posting, buying,
deleting, writing outside drafts) is left for the user: the report says what is ready and waiting.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

from servers._common import make_server
from servers.notes_reminders import parse_when
from iriis.storage import get_db

mcp = make_server("tasks")
TZ = ZoneInfo("Africa/Nairobi")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MIN_EVERY_S = 15 * 60


def _clock(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)?", s.strip())
    if not m:
        raise ValueError(f"Couldn't read the time '{s}'. Try 07:00 or 7am.")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3).startswith("p") else 0)
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError(f"'{s}' isn't a time of day.")
    return h, mi


def parse_repeat(repeat: str) -> str:
    """Normalise a schedule: 'daily 07:00', 'weekdays 7am', 'weekends 9:00', 'every monday 9am',
    'every 3 hours', 'every 30 minutes'. Returns the canonical form ('' = runs once)."""
    s = re.sub(r"\s+", " ", (repeat or "").strip().lower().replace(" at ", " "))
    if not s or s in ("once", "no", "none"):
        return ""
    m = re.fullmatch(r"every (\d+(?:\.\d+)?) ?(minutes?|mins?|hours?|hrs?)", s)
    if m:
        secs = float(m.group(1)) * (60 if m.group(2).startswith("m") else 3600)
        if secs < MIN_EVERY_S:
            raise ValueError("Routines can repeat at most every 15 minutes.")
        return f"every {int(secs)}s"
    m = re.fullmatch(r"(?:every )?(daily|day|every day|weekdays|weekday|weekends|weekend|" + "|".join(DAYS) + r")s? (.+)", s)
    if m:
        kind = {"day": "daily", "every day": "daily", "weekday": "weekdays", "weekend": "weekends"}.get(m.group(1), m.group(1))
        h, mi = _clock(m.group(2))
        return f"{kind} {h:02d}:{mi:02d}"
    raise ValueError(f"Couldn't read the schedule '{repeat}'. Try 'daily 07:00', 'weekdays 7am', "
                     "'every monday 9am' or 'every 3 hours'.")


def next_run(repeat: str, after: float) -> float | None:
    """Next time a canonical schedule fires strictly after `after` (epoch seconds)."""
    if not repeat:
        return None
    if repeat.startswith("every "):
        return after + float(repeat[6:-1])
    kind, hm = repeat.split(" ")
    h, mi = map(int, hm.split(":"))
    base = dt.datetime.fromtimestamp(after, TZ)
    for add in range(0, 8):
        d = (base + dt.timedelta(days=add)).replace(hour=h, minute=mi, second=0, microsecond=0)
        if d.timestamp() <= after:
            continue
        wd = d.weekday()
        if (kind == "daily" or (kind == "weekdays" and wd < 5) or (kind == "weekends" and wd >= 5)
                or (kind in DAYS and DAYS.index(kind) == wd)):
            return d.timestamp()
    return None


def _fmt(ts: float | None) -> str:
    if not ts:
        return "-"
    d = dt.datetime.fromtimestamp(ts, TZ)
    today = dt.datetime.now(TZ).date()
    day = ("today" if d.date() == today else "tomorrow" if d.date() == today + dt.timedelta(days=1)
           else "yesterday" if d.date() == today - dt.timedelta(days=1) else d.strftime("%a %d %b"))
    return f"{day} {d.strftime('%H:%M')}"


def _say_repeat(r: str) -> str:
    if not r:
        return "once"
    if r.startswith("every "):
        s = float(r[6:-1])
        return f"every {s / 3600:g} hours" if s >= 3600 else f"every {s / 60:g} minutes"
    kind, hm = r.split(" ")
    return f"{'every ' + kind.title() if kind in DAYS else kind} at {hm}"


@mcp.tool()
def start_task(goal: str, when: str = "now", repeat: str = "", title: str = "") -> str:
    """Hand Iriis a job to work on in the background (research, comparisons, drafting, checking things) and report
    back when done. goal: the full instruction, with every detail she'll need (she can't ask questions while
    working). when: "now", "in 2 hours", "17:30", "tomorrow 8am". repeat: makes it a routine, e.g. "daily 07:00",
    "weekdays 7am", "every monday 9am", "every 3 hours"."""
    goal = goal.strip()
    if len(goal) < 8:
        raise ValueError("Describe the task in a full sentence so I can work on it alone.")
    rep = parse_repeat(repeat)
    now = time.time()
    if when.strip().lower() in ("", "now", "right now", "immediately", "asap"):
        # a one-off or an "every N hours" routine starts now; "daily 07:00" waits for its first 07:00
        due = now if (not rep or rep.startswith("every ")) else next_run(rep, now)
    else:
        due = parse_when(when).timestamp()
    title = (title or goal.split(".")[0])[:80]
    tid = get_db().insert("INSERT INTO tasks (title, goal, repeat, status, next_run, runs, created) "
                          "VALUES (?, ?, ?, 'scheduled', ?, 0, ?)", (title, goal, rep, due, now))
    when_s = "now" if due <= now + 5 else _fmt(due)
    return (f"Task {tid} '{title}' starts {when_s}" + (f" and repeats {_say_repeat(rep)}" if rep else "") +
            ". I'll tell you when it's done.")


@mcp.tool()
def list_tasks(status: str = "") -> str:
    """Background tasks and routines with their status (scheduled, running, done, failed, cancelled), next run and
    the start of their latest report. status filters, e.g. "done" or "scheduled"."""
    q, args = "SELECT id, title, repeat, status, next_run, last_run, result, runs FROM tasks", ()
    if status:
        q, args = q + " WHERE status = ?", (status.strip().lower(),)
    rows = get_db().execute(q + " ORDER BY id DESC LIMIT 25", args)
    if not rows:
        return "No tasks yet." if not status else f"No {status} tasks."
    out = []
    for tid, title, rep, st, nxt, last, res, runs in rows:
        line = f"{tid}. {title} - {st}"
        if rep:
            line += f", {_say_repeat(rep)}, next {_fmt(nxt)}"
        elif st == "scheduled":
            line += f", starts {_fmt(nxt)}"
        if last:
            line += f", last ran {_fmt(last)}"
        if res and res.startswith("[in progress]"):
            line += f" (still working, session {runs + 1}): {res[14:240].strip()}..."
        elif res:
            line += f": {res[:140].strip()}{'...' if len(res) > 140 else ''}"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def task_result(task_id: int) -> str:
    """The full latest report of a background task."""
    rows = get_db().execute("SELECT title, status, last_run, result, goal FROM tasks WHERE id = ?", (int(task_id),))
    if not rows:
        return f"There's no task {task_id}."
    title, st, last, res, goal = rows[0]
    if not res:
        return f"Task {task_id} '{title}' is {st}; no report yet."
    return f"{title} ({st}, {_fmt(last)})\n\n{res}"


@mcp.tool()
def cancel_task(task_id: int) -> str:
    """Stop a background task or routine (it won't run again)."""
    rows = get_db().execute("UPDATE tasks SET status = 'cancelled', next_run = NULL WHERE id = ? "
                            "AND status IN ('scheduled', 'running', 'failed') RETURNING title", (int(task_id),))
    return f"Cancelled task {task_id}, {rows[0][0]}." if rows else f"Task {task_id} isn't scheduled or running."


@mcp.tool()
def run_task_now(task_id: int) -> str:
    """Run a scheduled task or routine right away (a routine then carries on with its schedule)."""
    rows = get_db().execute("UPDATE tasks SET status = 'scheduled', next_run = ? WHERE id = ? AND status <> 'running' "
                            "RETURNING title", (time.time(), int(task_id)))
    return f"Running task {task_id}, {rows[0][0]}, now." if rows else f"Task {task_id} is already running or doesn't exist."


# ---------------------------------------------------------------------------- phone calls and alarms
@mcp.tool()
def schedule_call(when: str, reason: str, repeat: str = "", opening: str = "") -> str:
    """Phone the user: the Iriis app rings like an incoming call (and a notification vibrates) and when they answer
    you talk. Use it for alarms ("wake me at 6:30"), important reminders, and whenever YOU judge a call is better
    than a message. when: "now", "in 20 minutes", "06:30", "tomorrow 7am". repeat: "daily 06:30", "weekdays 6am".
    reason: why you're calling (shown on the ringing screen). opening: optional first words when they pick up;
    leave it empty to decide what to say at that moment (better for alarms: you'll know the weather, the day...)."""
    reason = reason.strip()[:300] or "Call from Iriis"
    rep = parse_repeat(repeat)
    now = time.time()
    if when.strip().lower() in ("", "now", "right now", "immediately", "asap"):
        at = now
    elif rep and when.strip().lower() in (repeat.strip().lower(), "daily", "weekdays", "every day"):
        at = next_run(rep, now)
    else:
        at = parse_when(when).timestamp()
    cid = get_db().insert("INSERT INTO calls (at, reason, opening, status, repeat, source, created) "
                          "VALUES (?, ?, ?, 'scheduled', ?, 'user', ?)", (at, reason, opening.strip()[:600], rep, now))
    when_s = "now" if at <= now + 5 else _fmt(at)
    return f"Call {cid} set for {when_s}" + (f", repeating {_say_repeat(rep)}" if rep else "") + f": {reason}."


@mcp.tool()
def list_calls() -> str:
    """Alarms and calls that are coming up, and the last few that rang (answered, missed, declined)."""
    rows = get_db().execute("SELECT id, at, reason, status, repeat FROM calls WHERE status = 'scheduled' "
                            "OR at > ? ORDER BY at LIMIT 20", (time.time() - 2 * 86400,))
    if not rows:
        return "No alarms or calls set."
    return "\n".join(f"call {i}: {_fmt(at)} {reason} [{st}]" + (f" ({_say_repeat(r)})" if r else "")
                     for i, at, reason, st, r in rows)


@mcp.tool()
def cancel_call(call_id: int) -> str:
    """Cancel an alarm or a planned call (a repeating alarm stops for good)."""
    rows = get_db().execute("UPDATE calls SET status = 'cancelled' WHERE id = ? AND status IN ('scheduled', 'ringing') "
                            "RETURNING reason", (int(call_id),))
    return f"Cancelled call {call_id}: {rows[0][0]}." if rows else f"There's no upcoming call {call_id}."


if __name__ == "__main__":
    mcp.run()
