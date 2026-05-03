---
name: notes-obsidian
description: Read and write Obsidian vault notes — daily notes, link graph, tags, transclusions.
state: quarantined
capabilities:
  fs.read:
    - "~/Obsidian/**"
    - "~/vault/**"
    - "**/*.md"
  fs.write:
    - "~/Obsidian/**"
    - "~/vault/**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running notes-obsidian.

You work with an Obsidian vault — a markdown directory with backlinks,
tags, daily notes, transclusions. Treat it as a knowledge graph, not
just a folder of files.

Steps:
1. Detect the vault: check `~/Obsidian/`, `~/vault/`, or env
   `OBSIDIAN_VAULT`. Read `.obsidian/app.json` if present for daily
   note format / template config.
2. For READ: use the existing structure. Backlinks (`[[Note Name]]`)
   are first-class — when reading a note, follow at most one hop of
   backlinks unless the user asks to traverse.
3. For DAILY NOTE: append, don't overwrite. The daily note is a journal,
   not a draft surface. Use the user's existing template.
4. For NEW NOTE: kebab-case-or-Title-Case-as-the-vault-already-uses
   filenames. Backlink to relevant existing notes — search for nearby
   topics first.
5. For SEARCH: prefer tag/link search over full-text where the user's
   intent is clearly conceptual ("notes about X" → tag #x or link [[X]]).
6. For TAGS: don't invent new tag conventions; use what the vault
   already uses. List existing tags first if proposing a new one.

Never delete notes. Never rename a note that has backlinks without
following them and updating each. Treat the vault as the user's
exobrain — the integrity of the link graph matters more than tidiness.
