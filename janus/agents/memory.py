"""
agents/memory.py — per-agent persistent state.

WHY:
The global memory store (janus/memory.py + memory_cards.py) is the
USER's memory across the whole agent. When we add first-class agents
on 2026-05-11, each agent also needs its own narrow memory — facts
that agent learned, conversations it had, intermediate state it wants
to persist between invocations. Mixing those into the global store
would dilute "what does Janus know about Sam" with "what did the
research agent find about React in 2024".

DESIGN:
  * One directory per agent: ~/.janus/agents/<name>/memory/
  * kv.json    — structured key/value (atomic temp+rename writes)
  * notes.md   — free-form append-only notes the agent can write to
  * Per-conversation files written under ./conversations/<id>.json
    are reserved for future use; v1 only ships kv + notes.

P5 INVARIANT (plain-text everything):
All files are human-readable. No pickle, no SQLite. Sam can grep
through an agent's memory the same way he greps through his own.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .. import config


def _agent_memory_root(name: str) -> Path:
    """Where this agent's memory lives. Created on first write."""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)
    if not safe:
        raise ValueError("AgentMemory: invalid agent name")
    d = config.HOME / "agents" / safe / "memory"
    return d


class AgentMemory:
    """Per-agent kv + notes store.

    Two agents with the same name read/write the same files.
    Writes are atomic (temp-rename); read-modify-write under
    concurrent agents follows the same accepted limitation
    documented in Blackboard — last writer wins on key collision.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self._dir = _agent_memory_root(agent_name)

    # ---------- paths ----------

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def kv_path(self) -> Path:
        return self._dir / "kv.json"

    @property
    def notes_path(self) -> Path:
        return self._dir / "notes.md"

    # ---------- structured kv ----------

    def _load_kv(self) -> dict[str, Any]:
        p = self.kv_path
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_kv(self, data: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        p = self.kv_path
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, p)

    def get(self, key: str, default: Any = None) -> Any:
        return self._load_kv().get(key, default)

    def set(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        # Fail fast if the value isn't JSON-serializable.
        try:
            json.dumps(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"value not JSON-serializable: {e}")
        data = self._load_kv()
        data[key] = value
        self._save_kv(data)

    def delete(self, key: str) -> bool:
        data = self._load_kv()
        if key in data:
            del data[key]
            self._save_kv(data)
            return True
        return False

    def keys(self) -> list[str]:
        return sorted(self._load_kv().keys())

    def all(self) -> dict[str, Any]:
        return self._load_kv()

    # ---------- notes ----------

    def append_note(self, text: str) -> None:
        """Append a timestamped note to notes.md."""
        if not isinstance(text, str) or not text.strip():
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"\n## {ts}\n\n{text.strip()}\n"
        with open(self.notes_path, "a", encoding="utf-8") as f:
            f.write(line)

    def read_notes(self) -> str:
        if not self.notes_path.is_file():
            return ""
        try:
            return self.notes_path.read_text(encoding="utf-8")
        except OSError:
            return ""
