---
name: task-decomposer-with-budget
description: Decompose a task to fit a budget (time, cost USD, or turn count); ask if it can't fit.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/cost.jsonl"
    - "~/.janus/log.jsonl"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running task-decomposer-with-budget.

The user gave you a task AND a budget — time ("30 minutes"), cost
("under $0.50"), or turn count ("≤10 tool calls"). Your job is to
plan the decomposition to fit the budget OR push back if it can't.
Most agents ignore budgets; that's the differentiation.

Steps:
1. Identify the budget type and value. If the user didn't state one,
   propose a reasonable default and ask once. Don't proceed without
   a budget — the whole point.
2. ESTIMATE the unconstrained task: rough number of subtasks, tool
   calls per subtask, model cost per subtask (use cost-cartographer's
   per-task-type cost model if available).
3. Compare estimate vs budget:
   - FITS: proceed with the natural decomposition
   - 1-3× over: trim — drop nice-to-haves, batch where possible,
     downshift model on cheap subtasks
   - >3× over: STOP and ask. The task as stated doesn't fit. Offer
     2-3 reduced scopes and let the user pick.
4. EXECUTE within budget. Track cost / time / turns as you go.
   At 80% of budget, surface a checkpoint: "spent X of Y; on track
   to finish / will overshoot — should I cut Z to land under?"
5. REPORT the actual vs. estimate at the end. Calibration data goes
   into your decomposition for next time.

Don't silently overshoot. Don't degrade quality without a note ("I
used Haiku instead of Opus for the last step to land under budget").
The user trades latency for accuracy or cost — surface the trade.
