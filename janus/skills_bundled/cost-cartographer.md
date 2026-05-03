---
name: cost-cartographer
description: Map per-task LLM cost from cost.jsonl and recommend cheaper-model routing.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/cost.jsonl"
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

You are running cost-cartographer.

You read Janus's plain-text cost ledger and produce a per-task-type cost
map. Output recommends where to route to a cheaper model. Janus stores
cost as plain-text JSONL — that's the moat. Most agents store cost in
opaque dashboards; this skill exists because Janus does not.

Steps:
1. Read `~/.janus/cost.jsonl` (per-turn token + USD ledger).
2. Read `~/.janus/log.jsonl` for task context (the `request` field per
   record gives you the user's intent for that turn).
3. Group by inferred task type:
   - dev/code (touched .py, .js, .rs, .go in the same turn)
   - search/research (web.fetch, web.search dominated the tools)
   - chat (no tools, pure conversation)
   - admin/glue (git, ls, mv only)
4. For each group: median + p95 cost in USD, current model, total spend
   in the window, share of overall spend.
5. For each group where a frontier model is being used on a task type
   that doesn't need it (e.g., Opus for `git status` summarization),
   propose a cheaper alternative (Haiku, Sonnet, or local). Project
   the savings using the median × call-count.
6. Report a sorted table, biggest savings first. Include a one-line
   how-to for switching the model (env var or `/model`).

This is READ-ONLY. Never write to cost.jsonl. Never auto-switch the
model — recommend, don't apply. The user owns the cost/quality
trade-off decision.
