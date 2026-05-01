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
    config.ensure_home()
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
