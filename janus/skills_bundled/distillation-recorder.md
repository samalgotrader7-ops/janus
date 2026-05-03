---
name: distillation-recorder
description: EXPERIMENTAL — capture successful chains as fine-tune-ready records under ~/.janus/distill/.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/log.jsonl"
    - "~/.janus/conversations/**"
  fs.write:
    - "~/.janus/distill/**"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running distillation-recorder (EXPERIMENTAL).

⚠ KNOWN LIMITATION: there is no shipped fine-tune pipeline yet.
This skill only writes records; it does not train anything. The
output sits at `~/.janus/distill/` until a future Janus version (or a
user-built pipeline) consumes it.

You capture successful (multi-turn, tool-using) chains in a format
that's ready for fine-tuning a smaller model on the user's actual
patterns. Plain text, append-only, structured.

Steps:
1. IDENTIFY a successful chain. Heuristic: the user accepted the
   outcome (no follow-up correction, explicit "thanks"/"perfect", or
   the user moved on to a new topic). Read from `~/.janus/log.jsonl`
   and `conversations/`.
2. EXTRACT the chain into a fine-tune record:
   ```json
   {"messages": [{"role": "system", "content": "..."},
                 {"role": "user", "content": "..."},
                 {"role": "assistant", "content": "...",
                  "tool_calls": [...]},
                 {"role": "tool", "content": "..."},
                 ...],
    "task_type": "...", "model_used": "...", "skill": "..."}
   ```
3. REDACT — apply redaction-gateway rules. Distillation data is the
   training set; PII in it would propagate to the trained model.
4. WRITE to `~/.janus/distill/<task-type>-<date>.jsonl`. One record
   per line. Append-only.
5. REPORT count: "captured N chains across M task types this run".
   Don't claim any fine-tune happened — explicitly note the file
   awaits a future pipeline.

Read-only on logs. Append-only on distill/. Never claim a smaller
model is trained when the records are just sitting on disk. Surface
data quality issues (very short chains, repetitive patterns) — they
make bad training data.
