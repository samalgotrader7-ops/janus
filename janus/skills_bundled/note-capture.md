---
name: note-capture
description: Quick-capture notes to a file, inbox, or note app — minimal friction, no triage.
state: quarantined
capabilities:
  fs.write:
    - "~/notes/**"
    - "~/inbox/**"
    - "**/inbox.md"
    - "**/notes/**"
  fs.read:
    - "~/notes/**"
    - "~/inbox/**"
    - "**/inbox.md"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running note-capture.

The user wants to drop a thought into their note system as fast as
possible. Friction kills capture. Don't ask three questions to file
a one-line note.

Steps:
1. Detect the inbox: check for `~/notes/inbox.md`, `~/inbox.md`, or
   the configured Obsidian vault inbox. If none exists, propose one
   path and ask once.
2. Append the note with a timestamp prefix:
   `## 2026-05-03 14:32` followed by the note body. Use the user's
   exact wording — don't rewrite, summarize, or "improve" capture.
3. If the user mentioned a tag (#topic) or @person, preserve it
   verbatim in the note. Don't auto-add tags.
4. Confirm with one sentence: "Captured to ~/notes/inbox.md".

Never triage, summarize, or move notes during capture. Triage is a
separate skill (notes-obsidian, doc-create) with a different intent.
The whole point is speed of capture; preserve the user's words.
