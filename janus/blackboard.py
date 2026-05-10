"""
blackboard.py — internal shared state for Janus subagents
(v1.39.0, Phase 10.3.0).

WHY:
Sam's 4-ideas brief (2026-05-10) Layer A: when Janus orchestrates
multiple subagents (via subagent tool, swarms, or future agents),
they need a way to share state without each call re-passing all
the context. The classic AI architecture for this is a shared
"blackboard" — a free-form key-value store any agent can read or
write.

DESIGN (locked with Sam, 2026-05-10):
  * Free-form key-value JSON
  * Single-machine assumption — NO file locking
  * Atomic writes via temp-file + os.replace prevent partial-write
    corruption; race-conditioned overwrites between two agents
    stomping the same key are accepted as a known limitation
  * Storage at ~/.janus/blackboard/<run_id>.json — one file per
    run, callers pick the run_id

API:
  bb = Blackboard("my-run-id")
  bb.set("status", "in_progress")
  bb.set("results", [{"file": "foo.py", "ok": True}])
  bb.get("status")        → "in_progress"
  bb.keys()                → ["status", "results"]
  bb.all()                 → {"status": "in_progress", "results": [...]}
  bb.delete("status")      → True (or False if absent)
  bb.clear()               → None  (drops the whole blackboard)
  bb.path                  → ~/.janus/blackboard/my-run-id.json
  Blackboard.list_run_ids()  → list of all blackboard files on disk

TYPES:
Values must be JSON-serializable (str / int / float / bool / None /
list / dict of the same). We don't pickle to keep state plain-text-
inspectable (P5 invariant from Janus's design).

v1.39.1 adds the message bus (append-only messages.jsonl) +
swarm_message_send / swarm_message_recv tools that subagents call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config


def _root() -> Path:
    """Where blackboard files live. Created on demand."""
    d = config.HOME / "blackboard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_run_id(run_id: str) -> str:
    """Filesystem-safe run id. Replace path separators + colon."""
    if not run_id or not run_id.strip():
        raise ValueError("run_id required")
    safe = run_id.strip()
    for ch in (":", "/", "\\"):
        safe = safe.replace(ch, "_")
    return safe


class Blackboard:
    """Per-run shared key-value store for subagents.

    Two Blackboard instances with the same run_id read/write the
    same on-disk file. Each operation reads-modifies-writes the
    whole file (atomic via temp-rename) — fine for the typical
    workload (sequential subagent stages, modest blackboard size).
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = _safe_run_id(run_id)
        self._dir = _root()

    # ---------- paths ----------

    @property
    def path(self) -> Path:
        return self._dir / f"{self.run_id}.json"

    # ---------- low-level ----------

    def _load(self) -> dict[str, Any]:
        p = self.path
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _atomic_write(self, data: dict[str, Any]) -> None:
        p = self.path
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)

    # ---------- API ----------

    def get(self, key: str, default: Any = None) -> Any:
        return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        # Validate value is JSON-serializable. Fail fast — we'd
        # rather raise at set() than corrupt the file later.
        try:
            json.dumps(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"value not JSON-serializable: {e}")
        data = self._load()
        data[key] = value
        self._atomic_write(data)

    def delete(self, key: str) -> bool:
        data = self._load()
        if key in data:
            del data[key]
            self._atomic_write(data)
            return True
        return False

    def keys(self) -> list[str]:
        return sorted(self._load().keys())

    def all(self) -> dict[str, Any]:
        return self._load()

    def clear(self) -> None:
        """Drop the whole blackboard for this run."""
        if self.path.is_file():
            try:
                self.path.unlink()
            except OSError:
                pass

    def update(self, mapping: dict[str, Any]) -> None:
        """Merge mapping into the blackboard atomically — useful when
        a subagent has multiple values to write at once and we don't
        want to read+write N times."""
        if not isinstance(mapping, dict):
            raise TypeError("mapping must be a dict")
        for k in mapping.keys():
            if not isinstance(k, str) or not k:
                raise ValueError("all keys must be non-empty strings")
        # Validate every value once, up front.
        try:
            json.dumps(mapping)
        except (TypeError, ValueError) as e:
            raise ValueError(f"mapping not JSON-serializable: {e}")
        data = self._load()
        data.update(mapping)
        self._atomic_write(data)

    # ---------- discovery ----------

    @classmethod
    def list_run_ids(cls) -> list[str]:
        """Return sorted list of run_ids that currently have a
        blackboard file on disk."""
        d = _root()
        return sorted(p.stem for p in d.glob("*.json"))


# ---------- module-level convenience ----------


def get(run_id: str, key: str, default: Any = None) -> Any:
    return Blackboard(run_id).get(key, default)


def set_value(run_id: str, key: str, value: Any) -> None:
    """Use `set_value` (not `set` — Python builtin) to avoid shadowing."""
    Blackboard(run_id).set(key, value)


def delete(run_id: str, key: str) -> bool:
    return Blackboard(run_id).delete(key)


def all_for(run_id: str) -> dict[str, Any]:
    return Blackboard(run_id).all()


def clear(run_id: str) -> None:
    Blackboard(run_id).clear()
