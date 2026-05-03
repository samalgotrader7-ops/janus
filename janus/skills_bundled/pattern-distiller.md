---
name: pattern-distiller
description: Watch recent log turns, distill recurring user patterns into a draft quarantined skill.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/log.jsonl"
    - "~/.janus/skills/**"
    - "~/.janus/conversations/**"
  fs.write:
    - "~/.janus/skills/**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running pattern-distiller.

You watch the user's recent log turns, identify a recurring pattern
("Sam re-runs the same 4-step git flow weekly"), and propose a new
quarantined skill that captures it. Thin wrapper over Janus's existing
`skills.draft_skill_from_log` machinery — surface the proposal, don't
auto-promote (P4).

Steps:
1. Read the last N turns of `~/.janus/log.jsonl` (default N=50; user
   can override). Group by request similarity (recurring intent) and
   by tool-call sequence (recurring procedure).
2. Identify candidates: a pattern is a candidate if it's appeared
   ≥3 times AND the tool-call sequence is consistent. Less than that
   is noise.
3. Cross-check: does an existing skill already cover this? Read
   `~/.janus/skills/*.md` descriptions and compare via the same
   Jaccard match Janus uses internally. If covered, don't propose —
   note the existing skill instead.
4. For each NEW candidate, draft a skill:
   - kebab-case name from the dominant intent words
   - 1-line description matching what the user actually asked for
   - capabilities: the exact tool.verb tokens used in the recurring
     procedure (don't expand the surface — be conservative)
   - body: the procedure as numbered steps, in the user's idiom
5. Write the draft to `~/.janus/skills/<name>.md` as `state:
   quarantined`. Show the user the draft + the source turns. They
   read, decide whether to keep, edit, or delete.

Never auto-promote (P4 invariant). Never overwrite an existing skill
without explicit confirmation. If the user already has a skill for
this pattern, don't propose a duplicate — surface the existing one.
