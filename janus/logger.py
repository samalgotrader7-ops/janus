"""
logger.py — structured append-only log.

One JSON object per line in ~/.janus/log.jsonl.

WHY APPEND-ONLY JSONL:
  - Crash-safe: a half-written line is the only loss
  - grep-friendly, jq-friendly, easy to tail in another window
  - Trivial to ingest into SQLite later (Phase 2 FTS5 search)

WHAT'S IN A RECORD:
  - request, interpretations, choice, trace, output, feedback
  - timing for each phase
  - model, env (so analysis can compare runs across providers)
"""

from __future__ import annotations
import datetime
import json
from typing import Any

from . import config


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write(record: dict[str, Any]) -> None:
    """Append a JSON record. v1.11.0: scrubs PII / secrets via
    janus.redact before writing. JANUS_REDACT=off bypasses (back-compat
    with pre-v1.11 logs that callers may want to import 1:1)."""
    config.ensure_home()
    # Lazy import: redact pulls in re + os which are fine but the
    # logger is imported VERY early, and we want it unblockable.
    try:
        from . import redact
        record = redact.redact_obj(record)
    except Exception:
        # Failure-silent — better to log unredacted than crash on the
        # write path. We wouldn't want a redactor bug to nuke audit.
        pass
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_all() -> list[dict]:
    if not config.LOG_FILE.exists():
        return []
    out = []
    with config.LOG_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out
