"""Runs background tasks and routines (servers/tasks.py stores them) inside the cloud core.

Polls the tasks table, claims due tasks atomically, works through each one as an agent turn in its own session
(more tool steps than a voice turn, no confirmations possible), saves the report, schedules a routine's next run,
and tells the user through `notify` (voice announcement + Telegram when configured).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

from iriis.storage import DB, get_db

log = logging.getLogger("iriis.tasks")

TASK_BRIEF = (
    "[Background task {id}: the user is NOT listening right now; you are working on this alone. Plan the steps and "
    "work through them with your tools: search, read two or three good sources (not just snippets), cross-check "
    "numbers, and keep going until it's done or you hit a real blocker. You have a limited number of tool steps per "
    "session: once you've read enough to do a good job, stop reading and write the deliverable itself. If the "
    "deliverable is a document (a guide, plan, report, CV, article), write it in full and save it with make_document "
    "(pdf or docx), then name the file in your report; never say something is written, saved or drafted unless a "
    "tool actually saved it. You cannot ask questions or get "
    "confirmations now, so never send, post, buy, book or delete anything: prepare it (for example a draft) and say "
    "it is ready for the user to approve. Finish with a report the user can read later: the answer first, then the "
    "key details with where each came from (site names), then anything still needing the user. This report may be "
    "longer than a spoken reply; plain text, short paragraphs, no tables. End with one last line: 'STATUS: done' if "
    "the task is finished, or 'STATUS: continue' if you ran out of steps or time and should keep going in another "
    "session (then the report is your progress notes: what you've done and exactly what's left).]\n\nTask: {goal}"
)
CONTINUE = ("\n\n[You already worked on this in an earlier session. Your progress notes:\n{notes}\n"
            "Pick up exactly where you left off; don't redo what's done.]")
MAX_SESSIONS = 6      # a one-off task may continue over this many sessions before it reports what it has


def wants_more(report: str) -> tuple[bool, str]:
    """(should continue, report without the status line). The status line is the model's own judgement."""
    m = re.search(r"\n?\s*STATUS:\s*(done|continue)\s*\.?\s*$", report.strip(), re.I)
    if not m:
        return False, report.strip()
    return m.group(1).lower() == "continue", report.strip()[:m.start()].strip()


def first_sentence(text: str, limit: int = 220) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    s = m.group(1) if m else t
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "..."


class TaskRunner:
    def __init__(self, run: Callable[[int, str], Awaitable[str]], notify: Callable[[str, str], Awaitable[None]],
                 db: DB | None = None, interval_s: float = 15.0, max_parallel: int = 2, timeout_s: float = 900.0,
                 judge: Callable[[str, str], Awaitable[bool]] | None = None,
                 learn: Callable[[str, str], None] | None = None):
        """run(task_id, prompt) -> report text; notify(short spoken line, full report);
        judge(goal, report) -> True when the report is only progress notes (used when she leaves out STATUS)."""
        self.judge = judge
        self.learn = learn        # learn(goal, report): a finished task's lessons go into her playbook
        self.run_fn = run
        self.notify = notify
        self.db = db
        self.interval_s = interval_s
        self.max_parallel = max_parallel
        self.sem = asyncio.Semaphore(max_parallel)
        self.timeout_s = timeout_s
        self.active: set[asyncio.Task] = set()

    def _db(self) -> DB:
        return self.db or get_db()

    def recover(self) -> int:
        """After a redeploy: tasks left 'running' never finished; run them again."""
        rows = self._db().execute("UPDATE tasks SET status = 'scheduled', next_run = ? WHERE status = 'running' RETURNING id",
                                  (time.time(),))
        return len(rows)

    def due(self, limit: int) -> list[tuple[Any, ...]]:
        """Claim due tasks atomically (safe with two processes on one database)."""
        if limit <= 0:
            return []
        db = self._db()
        ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE status = 'scheduled' AND next_run <= ? "
                                         "ORDER BY next_run LIMIT ?", (time.time(), limit))]
        claimed = []
        for tid in ids:
            rows = db.execute("UPDATE tasks SET status = 'running', last_run = ? WHERE id = ? AND status = 'scheduled' "
                              "RETURNING id, title, goal, repeat, runs, result", (time.time(), tid))
            claimed += [tuple(r) for r in rows]
        return claimed

    def finish(self, tid: int, repeat: str, runs: int, report: str, ok: bool, more: bool = False) -> None:
        if more:      # unfinished one-off: keep the notes as progress and carry on in a minute
            self._db().execute("UPDATE tasks SET status = 'scheduled', result = ?, next_run = ?, runs = ? WHERE id = ? "
                               "AND status = 'running'", (f"[in progress] {report}", time.time() + 60, runs + 1, tid))
            return
        from servers.tasks import next_run
        nxt = next_run(repeat, time.time()) if repeat else None
        status = "scheduled" if nxt else ("done" if ok else "failed")
        self._db().execute("UPDATE tasks SET status = ?, result = ?, next_run = ?, runs = ? WHERE id = ? AND status = 'running'",
                           (status, report, nxt, runs + 1, tid))

    async def _work(self, row: tuple[Any, ...]) -> None:
        tid, title, goal, repeat, runs, previous = (*row, None)[:6]
        async with self.sem:
            t0 = time.time()
            more = False
            prompt = TASK_BRIEF.format(id=tid, goal=goal)
            if not repeat and previous:      # carrying on, or re-run by hand: start from what she already has
                prompt += CONTINUE.format(notes=str(previous).removeprefix("[in progress] ")[:6000])
                if runs >= 2:                  # research has a budget: from the third session, deliver
                    prompt += ("\n[This is session " + str(runs + 1) + ". Stop researching: write the finished "
                               "deliverable now from your notes, filling a gap only if it's critical.]")
            try:
                report = await asyncio.wait_for(self.run_fn(tid, prompt), self.timeout_s)
                stated = re.search(r"STATUS:\s*(done|continue)", report, re.I) is not None
                more, report = wants_more(report)
                if not stated and self.judge and not repeat:
                    try:
                        more = await self.judge(goal, report)
                    except Exception:  # noqa: BLE001
                        more = False
                more = more and not repeat and runs + 1 < MAX_SESSIONS
                ok = bool(report.strip())
                report = report.strip() or "I worked on it but ended without a report."
            except asyncio.TimeoutError:
                ok, report = False, f"I stopped after {self.timeout_s / 60:.0f} minutes without finishing."
            except Exception as e:  # noqa: BLE001
                ok, report = False, f"It failed: {e.__class__.__name__}: {str(e)[:300]}"
                log.exception("task %s failed", tid)
            await asyncio.to_thread(self.finish, tid, repeat, runs, report, ok, more)
            log.info("task %s %s in %.0fs", tid, "continues" if more else "done" if ok else "failed", time.time() - t0)
            if more:
                return        # keeps working quietly; the user hears when it's really done
            spoken = (f"Your task {title} is done. {first_sentence(report)}" if ok
                      else f"Your task {title} didn't finish. {first_sentence(report)}")
            if ok and self.learn:
                try:
                    self.learn(goal, report)
                except Exception:  # noqa: BLE001
                    log.exception("task %s: learning failed", tid)
            try:
                await self.notify(spoken, f"Task {tid}: {title}\n\n{report}")
            except Exception:  # noqa: BLE001
                log.exception("task %s notify failed", tid)

    async def run(self) -> None:
        try:
            n = await asyncio.to_thread(self.recover)
            if n:
                log.info("re-running %d task(s) interrupted by a restart", n)
        except Exception as e:  # noqa: BLE001
            log.warning("task recovery failed: %s", e)
        while True:
            try:
                free = self.max_parallel - sum(1 for t in self.active if not t.done())
                rows = await asyncio.to_thread(self.due, max(0, free))
            except Exception as e:  # noqa: BLE001  (database briefly unreachable)
                log.warning("task poll failed: %s", e)
                rows = []
            for row in rows:
                t = asyncio.create_task(self._work(row))
                self.active.add(t)
                t.add_done_callback(self.active.discard)
            await asyncio.sleep(self.interval_s)
