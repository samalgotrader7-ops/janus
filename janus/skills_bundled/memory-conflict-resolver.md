---
name: memory-conflict-resolver
description: When two memory diffs contradict, surface both with provenance; let the user pick.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/user.md"
    - "~/.janus/log.jsonl"
    - "~/.janus/conversations/**"
  fs.write:
    - "~/.janus/user.md.proposal"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running memory-conflict-resolver.

You handle the case where two memory updates disagree. Janus's memory
is plain markdown — like a `git merge` for facts. When proposed diff A
says "user prefers X" and proposed diff B says "user prefers not-X",
this skill surfaces the conflict instead of blindly applying the latest.

Steps:
1. Identify the conflict: take two (or N) proposed memory diffs as
   input — usually staged in `~/.janus/user.md.proposal` or coming
   from the memory.propose_diff pipeline.
2. For each diff, find its PROVENANCE: which conversation turn
   triggered it, what the user actually said. Cite verbatim.
3. Determine if it's a TRUE conflict or apparent:
   - TRUE: the two facts cannot both be current ("uses Vim" vs "uses
     Emacs daily")
   - APPARENT: the facts can coexist with context ("uses Vim for
     code" vs "uses Emacs for org-mode")
4. For TRUE conflicts, present the user with both options + provenance
   + a recency hint. They pick one, both, or neither. NEVER guess —
   memory drives agent behavior, the cost of being wrong is real.
5. For APPARENT conflicts, propose a merged entry that captures the
   nuance, attribute both source turns.
6. Write the resolution into `~/.janus/user.md.proposal`. The user
   reviews and applies.

Read-mostly on user.md. Proposal-only writes. Never blind-merge memory
— that erases the user's own historical statements.
