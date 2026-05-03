---
name: log-bisect
description: Bisect ~/.janus/log.jsonl to find the turn where a behavior changed (regression hunt).
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/log.jsonl"
    - "~/.janus/conversations/**"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running log-bisect.

The user says "the model started doing X recently — when?" Your job is
git-bisect-style: find the first turn where the behavior appeared,
across the plain-text JSONL log.

Steps:
1. Clarify the symptom precisely. "Started getting `git status` wrong"
   needs a TEST: a query you can replay against a turn and judge as
   pass/fail. Without a test you can't bisect — say so.
2. Define the bisect window: GOOD point (last known to behave
   correctly) and BAD point (first known to misbehave). Default to
   the last 200 turns if the user didn't specify.
3. Pick the midpoint. Read that turn from `log.jsonl` (record by
   record; line-oriented). Apply the test. Classify as GOOD or BAD.
4. Recurse: if midpoint was GOOD, bisect [midpoint, BAD]. If BAD,
   bisect [GOOD, midpoint]. Stop when window is 1 turn wide.
5. Report the FIRST BAD turn:
   - turn timestamp + conversation id
   - the user's request that triggered it
   - the model's response
   - any tool call args that look diagnostic
6. Suggest the next investigation step: "the conversation switched
   models here", "a hook was added at this point", "a skill was
   promoted just before this", "the user.md was edited here". Cross-
   reference the surrounding state changes.

Read-only. Never modify log.jsonl. Don't delete or rewrite history —
that destroys the audit trail this skill depends on.
