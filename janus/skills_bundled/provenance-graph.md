---
name: provenance-graph
description: Tag every artifact with conversation/model/prompt/inputs in a queryable sidecar graph.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/**"
    - "**"
  fs.write:
    - "~/.janus/provenance.jsonl"
    - "**/.provenance/**"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running provenance-graph.

Every file the agent writes — code, doc, image, report — is tagged
with where it came from: which conversation, which turn, which model,
which prompt, which inputs. Over time you build a queryable DAG that
answers "where did this paragraph come from?" months later.

Steps:
1. RECORD: for each artifact (file path) the agent created or
   meaningfully modified in this turn, append a record to
   `~/.janus/provenance.jsonl`:
   ```json
   {"ts": "...", "path": "...", "conv_id": "...", "turn": N,
    "model": "...", "user_request": "...", "tools_used": [...],
    "input_files": [...], "skill": "..." or null,
    "content_sha256": "..."}
   ```
2. ASK: when the user says "where did this come from?", read
   provenance.jsonl, find the most recent record for the path, and
   report the chain. Walk backward through `input_files` to build
   the DAG.
3. AUDIT: scan for orphaned or contradictory provenance — files that
   exist but have no record, files where the recorded sha doesn't
   match the current content (someone or something edited it
   out-of-band).
4. EXPORT: render the DAG as a Mermaid graph or DOT file when the
   user wants to see it visually.

Write-mostly. Records are append-only — never rewrite history. If a
file is deleted, mark a tombstone record but keep the original. The
provenance log is the integrity guarantee of every artifact under it.
