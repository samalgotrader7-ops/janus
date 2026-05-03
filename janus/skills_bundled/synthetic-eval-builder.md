---
name: synthetic-eval-builder
description: Turn real log.jsonl entries into eval datasets — mask the answer, keep the question.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/log.jsonl"
    - "~/.janus/conversations/**"
  fs.write:
    - "~/.janus/evals/**"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running synthetic-eval-builder.

You turn the user's actual usage history into an eval dataset. Each
real turn becomes one eval case: keep the user's prompt + the inputs,
hold out the assistant's response as the expected answer. Janus's
plain-text log is the source — Claude Code structurally cannot do
this either.

Steps:
1. SCOPE. Pick a window (last N days / per-skill / per-task-type).
   Larger isn't better; eval sets work best at 20-100 cases.
2. FILTER candidates: turns where the assistant's response was clearly
   useful (no follow-up correction from the user, no "actually no"
   in the next turn). Drop turns where the user re-asked or steered.
3. MASK each candidate:
   - keep: user request, model id at the time, mode, attached skill,
     non-secret context
   - hold out: the assistant's text response, tool call args (for
     tool-use evals)
   - REDACT: anything PII / secret per redaction-gateway rules
4. CATEGORIZE: tag each case with task type so eval reports can
   slice by category. Use the same categories cost-cartographer uses
   for consistency.
5. WRITE to `~/.janus/evals/synthetic-<date>.jsonl`. One case per
   line, JSON-encoded. Output a summary: N cases, M categories,
   estimated cost to replay the full set.
6. SUGGEST: `janus --eval --eval-set synthetic-<date>` (or whatever
   the harness's replay flag is) to run it.

Read-mostly on log + conversations. Never modify the source. Never
include unredacted PII in the eval set — these files ARE distributed
artifacts the user might share with collaborators.
