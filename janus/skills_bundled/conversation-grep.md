---
name: conversation-grep
description: Search across every saved conversation in plain-text JSON — grep, semantic, structured filters.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/conversations/**"
    - "~/.janus/log.jsonl"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running conversation-grep.

You search across the user's full conversation history. Janus stores
every conversation as plain JSON in `~/.janus/conversations/<id>.json` —
that's the moat. Claude Code, Cursor, and most agents structurally
cannot do this because their conversation state is opaque. You can.

Steps:
1. Inventory: list `~/.janus/conversations/*.json`. Each file is one
   conversation with `messages: [...]`, model id, last_updated, turn
   count.
2. Parse the user's query intent:
   - LITERAL — exact substring match across content
   - CONCEPTUAL — semantic / topic-based ("when did we discuss auth")
   - STRUCTURED — by tool used, by date range, by model, by skill
3. For LITERAL: walk every message, return matches with
   `<conversation-id>:<turn-index>` and a 2-line context window.
4. For CONCEPTUAL: read message content, score by topical match
   (cheap embedding via the model itself, or a re-rank on candidates
   that survived a literal pre-filter). Return ranked matches with
   one-sentence summaries.
5. For STRUCTURED: filter by metadata field. Examples: "all conversations
   that used the shell tool last week", "conversations with the
   'git-pr' skill attached".
6. Report: ranked list (max 10 unless user asks for more), each with
   the conversation-id and a one-line how-to-resume (`janus --resume
   <id>`).

Read-only. Never modify conversation files. Never delete a conversation
without explicit user instruction. The conversation log is the user's
exobrain — treat it as such.
