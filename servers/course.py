"""course MCP server: the book courses Iriis teaches (lessons texted at set times, questions she marks).

The lessons are written files (courses/<name>/lessons); this server reads them, reports progress, marks answers
and controls the schedule. The scheduler in the cloud core does the sending (iriis/agent/course.py).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from servers._common import make_server
from iriis.agent import course as C
from iriis.storage import get_db

mcp = make_server("course")
TZ = ZoneInfo("Africa/Nairobi")


def _course(name: str) -> C.Course:
    courses = C.all_courses()
    if not courses:
        raise ValueError("There are no courses.")
    if not name:
        return courses[0]
    for c in courses:
        if name.lower() in (c.name.lower(), c.title.lower()) or name.lower() in c.title.lower():
            return c
    raise ValueError(f"No course called {name!r}. Courses: " + ", ".join(c.title for c in courses))


@mcp.tool()
def status(course: str = "") -> str:
    """The courses you teach the user (a book turned into lessons you text twice a day) and where they are: lessons
    sent, the next one and when, the book's file id (read or search it with studio__inspect_file), and the
    user's marked answers so far."""
    out = []
    for c in ([_course(course)] if course else C.all_courses()):
        st = C.state(c.name)
        book = C.book_file(c)
        book_line = f"Book: file {book[0]} ({book[1]})" if book else f"Book: not uploaded yet (looking for '{c.book}')"
        if not st:
            out.append(f"{c.title}: not started ({len(c.lessons)} lessons). {book_line}.")
            continue
        sent = sorted(int(k) for k in st.get("sent", {}))
        nxt = st["next"]
        if nxt > len(c.lessons):
            when = "finished"
        elif st.get("paused"):
            when = f"paused before lesson {nxt}"
        else:
            at = C.next_send(c, max(time.time(), st["started"]), TZ)
            when = f"lesson {nxt} ({c.lesson(nxt).title}) comes {at.strftime('%a %d %b, %H:%M')}"
        answers = st.get("answers", [])
        score = (f"{len(answers)} answers marked, average {sum(a['grade'] for a in answers) / len(answers):.1f}/5"
                 if answers else "no answers marked yet")
        last = f"; last sent: lesson {sent[-1]} ({c.lesson(sent[-1]).title})" if sent else ""
        out.append(f"{c.title}: {len(sent)} of {len(c.lessons)} lessons sent{last}; {when}. {score}. {book_line}.")
    return "\n".join(out)


@mcp.tool()
def lesson(n: int, course: str = "", with_key: bool = True) -> str:
    """One lesson in full, as it was (or will be) sent, plus its answer key for marking the user's answers
    (with_key). Use it to mark answers, to resend or recap a lesson, and to answer questions about it."""
    c = _course(course)
    ls = c.lesson(int(n))
    text = C.message(c, ls.n)
    if with_key and ls.key:
        text += "\n\n## Answer key (for you; don't paste it, use it to mark and explain)\n" + ls.key
    return text


@mcp.tool()
def search(query: str, course: str = "") -> str:
    """Search every lesson's text, notes and answer keys for a word or phrase (a person, a case, a term like FOER
    or 13F). Returns the lessons where it appears with the line. For anything the lessons don't cover, search the
    book itself with studio__inspect_file(file_id, find=...)."""
    c = _course(course)
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2] or [query.lower()]
    hits = []
    for ls in c.lessons:
        for line in (ls.body + "\n" + ls.key).splitlines():
            low = line.lower()
            if all(w in low for w in words):
                hits.append(f"Lesson {ls.n} ({ls.title}): {line.strip()[:300]}")
    if not hits:
        book = C.book_file(c)
        return (f"'{query}' isn't in the lessons." +
                (f" Search the book: studio__inspect_file(file_id={book[0]}, find='{query}')." if book else ""))
    return "\n".join(hits[:20]) + (f"\n(and {len(hits) - 20} more)" if len(hits) > 20 else "")


@mcp.tool()
def record_answer(lesson_n: int, question: int, grade: int, feedback: str, course: str = "") -> str:
    """Keep the mark after you've marked one of the user's answers: grade 1-5 (5 = covers the answer key and
    applies it well) and a one-line note on what was strong or missing. Builds their progress report."""
    c = _course(course)
    st = C.state(c.name) or C.start(c.name)
    st.setdefault("answers", []).append({"lesson": int(lesson_n), "q": int(question), "grade": max(1, min(5, int(grade))),
                                         "note": feedback[:300], "at": time.time()})
    C.save(c.name, st)
    return f"Recorded: lesson {lesson_n}, question {question}, {grade}/5."


@mcp.tool()
def control(action: str, lesson_n: int = 0, course: str = "") -> str:
    """Change the schedule when the user asks: 'start' (begin, first lesson at the next send time), 'send_now'
    (the next lesson arrives within a minute), 'pause', 'resume', 'goto' (lesson_n is sent next), 'restart'."""
    c = _course(course)
    db = get_db()
    st = C.state(c.name, db)
    a = action.strip().lower()
    if a in ("start", "restart") or st is None:
        st = C.start(c.name, db)
        if a == "send_now":
            st["send_now"] = True
            C.save(c.name, st, db)
            return f"{c.title} started: lesson 1 arrives within a minute."
        at = C.next_send(c, time.time(), TZ)
        return f"{c.title} started: lesson 1 comes {at.strftime('%a %d %b, %H:%M')}, then at {' and '.join(c.times)} daily."
    if a == "send_now":
        if st["next"] > len(c.lessons):
            return "The course is finished; every lesson has been sent. Use goto to resend one."
        st["send_now"], st["paused"] = True, False
    elif a == "pause":
        st["paused"] = True
    elif a == "resume":
        st["paused"] = False
    elif a == "goto":
        c.lesson(int(lesson_n))
        st["next"] = int(lesson_n)
    else:
        raise ValueError("action is start, send_now, pause, resume, goto or restart")
    C.save(c.name, st, db)
    nxt = st["next"]
    return {"send_now": f"Lesson {nxt} arrives within a minute.", "pause": f"Paused; lesson {nxt} waits.",
            "resume": f"Resumed; lesson {nxt} comes at the next send time.",
            "goto": f"Lesson {nxt} comes next."}[a] + f" ({datetime.now(TZ).strftime('%H:%M')} now)"


if __name__ == "__main__":
    mcp.run()
