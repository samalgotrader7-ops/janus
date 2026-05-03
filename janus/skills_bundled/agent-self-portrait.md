---
name: agent-self-portrait
description: Read Janus's own state directory and produce a one-page "what kind of agent am I right now" report.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/**"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running agent-self-portrait.

You read Janus's full state directory and produce a one-page portrait
of the agent the user has shaped. Skills, trust scores, top-used
patterns, cost trajectory, memory highlights, hook configuration. The
plain-text state directory is the moat — this skill is structurally
impossible elsewhere.

Steps:
1. Walk `~/.janus/`:
   - `skills/*.md` — count, by state, top 5 by trust score, top 5 by
     run count
   - `log.jsonl` — last 30 days of activity, top tools, top tasks
   - `cost.jsonl` — total spend last 30 days, current model, model
     mix
   - `user.md` (memory) — first-paragraph distillation
   - `conversations/` — count, total turns, longest conversation
   - `hooks.json` (if present) — count of hooks, types
   - `mcp/servers.json` — connected servers
2. Identify the AGENT'S CHARACTER:
   - dominant skills used (signals what the user actually does)
   - dominant tools used (signals what the agent actually executes)
   - permission mode mix (signals the trust the user extends)
   - cost per task type (signals where the user invests compute)
3. Identify GAPS:
   - skills imported but never promoted (clutter)
   - skills promoted but never used (probably misaligned)
   - capability denials in logs (the agent keeps trying X but isn't
     allowed — propose either denying explicitly or granting cleanly)
4. Render a one-page report:
   - "Janus, configured for <user persona>"
   - 3-5 bullet character sketch
   - Top 5 skills (with trust)
   - Cost summary
   - Recommendations (3 actions the user could take)

Read-only. This is reflection, not modification. Never auto-prune
"unused" skills. Never auto-promote anything. Surface, don't act.
