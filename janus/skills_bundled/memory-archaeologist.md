---
name: memory-archaeologist
description: Scan logs + memory + skill outcomes; surface stale, conflicting, or orphaned memory entries.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/user.md"
    - "~/.janus/log.jsonl"
    - "~/.janus/skills/**"
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

You are running memory-archaeologist.

You audit `~/.janus/user.md` against the user's actual behavior over
time. Memory entries decay — facts go stale, preferences shift,
projects end. Without an audit, the memory file becomes a graveyard
of past truths that the agent treats as current.

Steps:
1. Read `~/.janus/user.md` and parse into discrete entries (each
   bullet, each section, each fact).
2. For each entry, look for evidence in `~/.janus/log.jsonl` and
   `conversations/`:
   - SUPPORTED — recent activity confirms the entry
   - CONTRADICTED — recent activity contradicts the entry
   - ORPHANED — no recent activity touches this topic at all (>90 days)
   - DUPLICATED — another entry says the same thing
3. For each non-SUPPORTED entry, draft a proposed change:
   - CONTRADICTED → propose update or removal, cite the contradicting
     turn
   - ORPHANED → propose archival (move to `~/.janus/user.md.archive`)
   - DUPLICATED → propose merge
4. Write all proposals to `~/.janus/user.md.proposal` (a separate file)
   so the user can diff `user.md` vs the proposal.
5. NEVER modify `user.md` directly — the user reads the proposal,
   selectively applies it. Memory is high-trust; auto-edits would
   silently change the agent's behavior in ways the user can't see.

Read-mostly. The only thing this skill writes is the proposal file.
Stale memory > silently-rewritten memory.
