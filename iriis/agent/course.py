"""Courses: a book turned into lessons that Iriis texts at set times (e.g. 10:00 and 22:00), one after another.

A course is a folder in courses/: course.yaml (title, the book's file name, send times, chapter names) and
lessons/NN.md (front matter, then The lesson / Notes / Try it / Questions / Answer key). Everything before
"## Answer key" is sent; the key stays with Iriis for marking answers. Progress lives in the kv table, so a
redeploy never resends or skips a lesson: each send time is claimed once (course:<name>:slot:<date time>).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from iriis.storage import DB, get_db

log = logging.getLogger("iriis.course")
COURSES_DIR = Path(__file__).resolve().parents[2] / "courses"
KEY_SPLIT = re.compile(r"^## Answer key\s*$", re.M | re.I)
QUIET_START, QUIET_END = 22.5, 7.0   # no first lesson in the middle of the night: it waits for the next send time


@dataclass
class Lesson:
    n: int
    chapter: int
    title: str
    body: str          # what she sends
    key: str           # the answer key (never sent)


@dataclass
class Course:
    name: str
    title: str
    book: str = ""                 # part of the uploaded book's file name, to find it in the files table
    times: list[str] = field(default_factory=lambda: ["10:00", "22:00"])
    autostart: bool = False
    chapters: dict[int, str] = field(default_factory=dict)
    lessons: list[Lesson] = field(default_factory=list)

    def lesson(self, n: int) -> Lesson:
        for ls in self.lessons:
            if ls.n == n:
                return ls
        raise ValueError(f"{self.title} has lessons 1-{len(self.lessons)}; there's no lesson {n}.")

    def day(self, n: int) -> int:
        """The course day lesson n belongs to (its content), whenever it is actually sent."""
        return (n - 1) // max(1, len(self.times)) + 1

    def slot_label(self, n: int) -> str:
        per_day = max(1, len(self.times))
        day, i = (n - 1) // per_day + 1, (n - 1) % per_day
        hh = int(self.times[i].split(":")[0])
        when = f"{hh % 12 or 12} {'am' if hh < 12 else 'pm'}"
        return f"Day {day} · {when}"


def parse_lesson(text: str) -> Lesson:
    meta: dict[str, Any] = {}
    if text.startswith("---"):
        _, front, text = text.split("---", 2)
        # plain "key: value" lines (titles often contain colons, which strict YAML rejects)
        meta = {k.strip(): v.strip().strip('"') for k, _, v in
                (ln.partition(":") for ln in front.splitlines() if ":" in ln)}
    parts = KEY_SPLIT.split(text, maxsplit=1)
    return Lesson(n=int(meta["n"]), chapter=int(meta.get("chapter", 0)), title=str(meta.get("title", "")).strip(),
                  body=parts[0].strip(), key=parts[1].strip() if len(parts) > 1 else "")


def load(name: str, root: Path = COURSES_DIR) -> Course:
    d = root / name
    cfg = yaml.safe_load((d / "course.yaml").read_text(encoding="utf-8")) or {}
    lessons = sorted((parse_lesson(p.read_text(encoding="utf-8")) for p in (d / "lessons").glob("*.md")),
                     key=lambda ls: ls.n)
    return Course(name=name, title=cfg.get("title", name), book=cfg.get("book", ""),
                  times=[str(t) for t in cfg.get("times", ["10:00", "22:00"])], autostart=bool(cfg.get("autostart")),
                  chapters={int(k): str(v) for k, v in (cfg.get("chapters") or {}).items()}, lessons=lessons)


def all_courses(root: Path = COURSES_DIR) -> list[Course]:
    if not root.is_dir():
        return []
    return [load(p.name, root) for p in sorted(root.iterdir()) if (p / "course.yaml").is_file()]


def message(c: Course, n: int) -> str:
    """The text she sends for lesson n."""
    ls = c.lesson(n)
    chap = c.chapters.get(ls.chapter, "")
    head = (f"Lesson {n} of {len(c.lessons)} · Day {c.day(n)} of {c.day(len(c.lessons))} · {c.title}\n"
            f"Chapter {ls.chapter}{': ' + chap if chap else ''}\n**{ls.title}**")
    tail = ("Answer the questions here whenever you like (I'll mark them), or ask me anything about the book."
            if n < len(c.lessons) else "That's the whole book. Answer these, and ask me anything about any chapter.")
    return f"{head}\n\n{ls.body}\n\n{tail}"


# ---------------------------------------------------------------------------- progress (kv table)
def _get(db: DB, key: str) -> str | None:
    rows = db.execute("SELECT value FROM kv WHERE key = ?", (key,))
    return rows[0][0] if rows else None


def _put(db: DB, key: str, value: str) -> None:
    db.execute("INSERT INTO kv (key, value, updated) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
               "value = excluded.value, updated = excluded.updated", (key, value, time.time()))


def _claim(db: DB, key: str) -> bool:
    """True once per key, even with two processes polling the same database."""
    return bool(db.execute("INSERT INTO kv (key, value, updated) VALUES (?, '1', ?) ON CONFLICT (key) DO NOTHING "
                           "RETURNING key", (key, time.time())))


def state(name: str, db: DB | None = None) -> dict[str, Any] | None:
    raw = _get(db or get_db(), f"course:{name}")
    return json.loads(raw) if raw else None


def save(name: str, st: dict[str, Any], db: DB | None = None) -> None:
    _put(db or get_db(), f"course:{name}", json.dumps(st))


def start(name: str, db: DB | None = None, at: int = 1, now: float | None = None) -> dict[str, Any]:
    st = {"next": at, "started": time.time() if now is None else now, "paused": False, "sent": {}, "answers": [], "send_now": False}
    save(name, st, db)
    return st


def book_file(c: Course, db: DB | None = None) -> tuple[int, str] | None:
    """The newest uploaded file whose name contains the course's book name (spaces and underscores alike)."""
    if not c.book:
        return None
    pat = "%" + re.sub(r"[\s_]+", "%", c.book.strip()) + "%"
    rows = (db or get_db()).execute("SELECT id, name FROM files WHERE name LIKE ? ORDER BY id DESC LIMIT 1", (pat,))
    return (int(rows[0][0]), rows[0][1]) if rows else None


def next_send(c: Course, after: float, tz: ZoneInfo) -> datetime:
    t = datetime.fromtimestamp(after, tz)
    for day in range(3):
        d = (t + timedelta(days=day)).date()
        for hm in sorted(c.times):
            h, m = map(int, hm.split(":"))
            cand = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            if cand.timestamp() > after:
                return cand
    return t


def _last_slot(c: Course, now: float, tz: ZoneInfo) -> datetime | None:
    t = datetime.fromtimestamp(now, tz)
    slots = []
    for day in (0, 1):
        d = (t - timedelta(days=day)).date()
        for hm in c.times:
            h, m = map(int, hm.split(":"))
            slots.append(datetime(d.year, d.month, d.day, h, m, tzinfo=tz))
    past = [s for s in slots if s.timestamp() <= now]
    return max(past) if past else None


def due(c: Course, now: float, tz: ZoneInfo, db: DB | None = None) -> list[int]:
    """Lessons to send now (and the progress recorded). Called by the scheduler every half minute."""
    db = db or get_db()
    st = state(c.name, db)
    if st is None:
        if not c.autostart:
            return []
        st = start(c.name, db, now=now)
        hour = datetime.fromtimestamp(now, tz)
        h = hour.hour + hour.minute / 60
        if not (QUIET_END <= h < QUIET_START) or not _claim(db, f"course:{c.name}:slot:start"):
            return []                   # night: lesson 1 waits for the next send time
        return _send(c, st, now, db)
    if st.get("paused") or st["next"] > len(c.lessons):
        return []
    if st.get("send_now"):              # "send me the next lesson now"
        st["send_now"] = False
        return _send(c, st, now, db)
    slot = _last_slot(c, now, tz)
    if slot is None or slot.timestamp() <= st["started"]:
        return []
    if not _claim(db, f"course:{c.name}:slot:{slot.strftime('%Y-%m-%d %H:%M')}"):
        return []
    return _send(c, st, now, db)


def _send(c: Course, st: dict[str, Any], now: float, db: DB) -> list[int]:
    n = st["next"]
    if n > len(c.lessons):
        save(c.name, st, db)
        return []
    st["sent"][str(n)] = now
    st["next"] = n + 1
    save(c.name, st, db)
    return [n]


async def run_courses(core, every: float = 30.0, root: Path = COURSES_DIR) -> None:
    """Texts each lesson at its time: in the chat, Telegram and a notification (Mind.say)."""
    if os.environ.get("IRIIS_NO_COURSES"):
        return
    tz = ZoneInfo(core.cfg.timezone or "Africa/Nairobi")
    courses = await asyncio.to_thread(all_courses, root)
    while True:
        for c in courses:
            try:
                for n in await asyncio.to_thread(due, c, time.time(), tz, core.db):
                    await core.mind.say(message(c, n))
                    log.info("course %s: sent lesson %d", c.name, n)
            except Exception as e:  # noqa: BLE001
                log.warning("course %s: %s", c.name, e)
        await asyncio.sleep(every)
