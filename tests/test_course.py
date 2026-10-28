"""Book courses: lessons sent once each at their times, never twice, never at night, answer keys kept back."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from iriis.agent import course as C
from iriis.storage import DB

TZ = ZoneInfo("Africa/Nairobi")
LESSON = """---
n: {n}
chapter: {ch}
title: Lesson {n}: the {n}th idea
---
## The lesson
Body of lesson {n}.

## Questions
1. Why?

## Answer key
- Secret key {n}.
"""


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


@pytest.fixture()
def course(tmp_path):
    d = tmp_path / "demo"
    (d / "lessons").mkdir(parents=True)
    (d / "course.yaml").write_text('title: Demo course\nbook: Some_Book\ntimes: ["10:00", "22:00"]\nautostart: true\n'
                                   'chapters:\n  1: "One: the start"\n  2: Two\n', encoding="utf-8")
    for n in range(1, 5):
        (d / "lessons" / f"{n:02d}.md").write_text(LESSON.format(n=n, ch=(n + 1) // 2), encoding="utf-8")
    return C.load("demo", tmp_path), DB("", tmp_path / "t.db")


def test_lessons_parse_and_the_key_is_never_sent(course):
    c, _ = course
    assert [ls.n for ls in c.lessons] == [1, 2, 3, 4]
    assert c.lesson(1).title == "Lesson 1: the 1th idea"          # a colon in the title is fine
    msg = C.message(c, 3)
    assert msg.startswith("Day 2 · 10 am · Lesson 3 of 4 · Demo course\nChapter 2: Two")
    assert "Body of lesson 3" in msg and "Secret key" not in msg and "Answer key" not in msg
    assert c.slot_label(4) == "Day 2 · 10 pm"


def test_schedule_sends_each_lesson_once_at_its_time(course):
    c, db = course
    assert C.due(c, at(2, 9, 0), TZ, db) == [1]            # autostart in the day: lesson 1 right away
    assert C.due(c, at(2, 9, 1), TZ, db) == []
    assert C.due(c, at(2, 10, 0), TZ, db) == [2]           # 10:00
    assert C.due(c, at(2, 10, 5), TZ, db) == []            # same slot: never twice (also across restarts)
    assert C.due(c, at(2, 21, 59), TZ, db) == []
    assert C.due(c, at(2, 22, 1), TZ, db) == [3]
    assert C.due(c, at(3, 18, 0), TZ, db) == [4]           # server down at 10:00: sent late, not skipped
    assert C.due(c, at(3, 22, 0), TZ, db) == []            # finished
    assert set(C.state("demo", db)["sent"]) == {"1", "2", "3", "4"}


def test_first_lesson_waits_for_morning(course):
    c, db = course
    assert C.due(c, at(2, 2, 0), TZ, db) == []             # deployed at 2 am: no lesson at night
    assert C.due(c, at(2, 9, 59), TZ, db) == []
    assert C.due(c, at(2, 10, 0), TZ, db) == [1]


def test_send_now_pause_and_book_lookup(course):
    c, db = course
    C.start("demo", db, now=at(2, 14, 0))
    st = C.state("demo", db)
    st["send_now"] = True
    C.save("demo", st, db)
    assert C.due(c, at(2, 15, 0), TZ, db) == [1]
    st = C.state("demo", db)
    st["paused"] = True
    C.save("demo", st, db)
    assert C.due(c, at(2, 22, 0), TZ, db) == []
    db.insert("INSERT INTO files (name, mime, size, data, created) VALUES (?, ?, ?, ?, ?)",
              ("_Some Book - Author.md", "text/markdown", 1, "", 0))
    assert C.book_file(c, db)[1] == "_Some Book - Author.md"


def test_course_tools_on_the_real_course(tmp_path, monkeypatch):
    """The family office course as shipped: every lesson parses, and her tools work on it."""
    from servers import course as S
    d = DB("", tmp_path / "c.db")
    monkeypatch.setattr(S, "get_db", lambda: d)
    monkeypatch.setattr(C, "get_db", lambda: d)
    c = C.load("family-office")
    assert c.lessons and all(ls.body and ls.key and "## Questions" in ls.body for ls in c.lessons)
    assert "not started" in S.status()
    assert "started" in S.control("start")
    full = S.lesson(1)
    assert "Answer key" in full and "Lesson 1 of" in full
    assert "Lesson 1" in S.search("Rockefeller")
    assert "Recorded" in S.record_answer(1, 1, 4, "good on thresholds, missed coordinator offices")
    assert "1 answers marked, average 4.0/5" in S.status()
    assert "arrives within a minute" in S.control("send_now")
