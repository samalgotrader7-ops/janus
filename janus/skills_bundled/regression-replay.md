---
name: regression-replay
description: Re-run a fixed eval set against current model+prompts to detect behavior regressions.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/evals/**"
    - "~/.janus/log.jsonl"
  fs.write:
    - "~/.janus/evals/**"
  shell.exec:
    - "janus --eval*"
    - "janus -p *"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running regression-replay.

You hold the agent's behavior to a fixed eval set across model upgrades
and prompt changes. Janus's `--eval` subcommand replays log records at
temp=0; this skill orchestrates targeted regression checks rather than
full re-replays.

Steps:
1. Identify what to regression-check. Three modes:
   - MODEL CHANGE: user just switched models; replay last 50 turns to
     detect new wrong answers
   - SKILL CHANGE: user just edited or promoted a skill; replay
     records that used that skill
   - PROMPT CHANGE: user edited the system prompt or memory; replay
     a curated golden set
2. Load the eval set. Eval files live at `~/.janus/evals/*.jsonl` —
   if there's no curated set yet, propose one (copy 20 high-confidence
   past turns into `evals/golden.jsonl`).
3. Replay: `janus --eval --last 50` or `janus --eval --skill <name>`.
   Capture the report (Janus produces interp_drift_avg automatically).
4. Compare against the previous run. The user keeps a baseline at
   `~/.janus/evals/baseline.json` — diff against it.
5. Surface regressions: turns that USED to behave consistently and now
   diverge. Quote the old vs new response. Do NOT auto-update the
   baseline — the user reads the diff and decides.

Read-only on log.jsonl. Don't modify the eval set without confirmation
— it's the ground truth this whole skill rests on.
