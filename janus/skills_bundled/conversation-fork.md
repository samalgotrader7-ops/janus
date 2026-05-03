---
name: conversation-fork
description: Replay a past conversation step-by-step, fork from any turn with a different model/mode/skill.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/conversations/**"
    - "~/.janus/log.jsonl"
  fs.write:
    - "~/.janus/conversations/**"
  shell.exec:
    - "janus --resume *"
    - "janus -p *"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running conversation-fork.

You take a past conversation, replay it, and at any chosen turn you
fork: re-run from that point with a different model, mode, or attached
skill. Then you compare outcomes side-by-side. Janus's plain-text
conversation files make this possible — the moat.

Steps:
1. Identify the source conversation: `~/.janus/conversations/<id>.json`
   — confirm with the user (id, last_updated, turn count, summary).
2. Pick the FORK POINT: a turn index in the original conversation.
   Show the user that turn's user prompt + the assistant's response so
   they can confirm "fork after this".
3. Build the fork: copy the conversation file, truncate at the fork
   point (drop everything after the chosen turn), save as a new
   conversation id (`fork-<original-id>-<n>`).
4. Re-run from the fork point with the user's chosen variation:
   - different model (`JANUS_MODEL=...`)
   - different mode (default / acceptEdits / plan / bypassPermissions)
   - different skill attached (`--skill <name>`)
   - different user prompt (let them rephrase the question)
5. Compare: show the original turn's response vs the fork's response
   side by side. Note where they diverged in tool calls, in claims,
   in length.
6. Report: the new fork's id (so user can `janus --resume fork-...`),
   the divergence summary, and a one-line takeaway.

Never modify the ORIGINAL conversation file. Forks are always copies.
Never auto-promote or auto-merge a fork's outcome back into the
original. The fork is a new branch in the user's history.
