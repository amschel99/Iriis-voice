---
name: long-task-planning
description: How to run a long, multi-step job (a background task, a project, anything over many tool steps or sessions) so it never loses the thread - a written plan, findings saved as you go, errors logged, failures never repeated.
keywords: [background task, project, plan, multi step, multi-step, step by step, long task, over several days, keep working, continue, routine, work on this, take your time, while i'm away]
servers: [tasks]
source: adapted from OthmanAdi/planning-with-files (MIT) — the rules; her progress notes play the role of the plan files
---
# Long-task planning

Your context is short-term memory; your progress notes (and memory) are the disk. Anything important goes on disk.

1. **Plan first.** Before the first tool call of a long job, write the plan: the goal in one line, then phases
   (`Phase 1: ... [pending]`), and what "done" looks like. In a background task the plan is the top of your progress
   notes; in a chat, say it in one line and start.
2. **The two-action rule.** After every two searches or page reads, write down the key findings (numbers, names,
   links). Things you saw but didn't write down are gone next session.
3. **Read before you decide.** Before a big decision or at the start of a new session, reread the plan and findings.
   Pick up exactly where they say; don't redo finished phases.
4. **Update after you act.** When a phase finishes, mark it `[complete]` and name the single next step.
5. **Log every error** with what you tried: `Error: <what> | Tried: <what> | Result: <what>`.
6. **Never repeat a failure.** If an action failed, the next attempt must be different. Three strikes:
   1st: read the error and fix the cause. 2nd: a different method or tool. 3rd: rethink the approach.
   After three: stop and report to the user what you tried and what you need.
7. **Finish properly.** The deliverable itself (not notes about it), saved with a tool if it's a document, then the
   report. New work after "done" becomes a new phase, not a rewrite.

Progress notes layout for a background task:
```
Goal: ...
Phases: 1 ... [complete] / 2 ... [in progress] / 3 ... [pending]
Findings: - fact (source) ...
Errors: - ...
Next step: ...
```
