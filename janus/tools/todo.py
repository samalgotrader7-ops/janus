"""
tools/todo.py — Phase 9: agent-facing TODO list (mirrors Claude Code TodoWrite/Read).

State lives at config.TODOS_FILE (~/.janus/todos.json). Plain JSON, the
user can `cat`/`jq`/edit by hand. Read/write are non-dangerous — these
are scratch notes, not user data.
"""

from __future__ import annotations
import json
from typing import Callable

from . import base
from .. import config


_VALID_STATUSES = ("pending", "in_progress", "completed")


def _read() -> list[dict]:
    p = config.TODOS_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(items: list[dict]) -> None:
    config.ensure_home()
    config.TODOS_FILE.write_text(
        json.dumps(items, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


class TodoRead(base.Tool):
    name = "todo_read"
    description = (
        "Read the agent's working todo list. Returns one line per item, "
        "tagged with status. Returns '(no todos)' when empty."
    )
    parameters = {"type": "object", "properties": {}}
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        items = _read()
        if not items:
            return "(no todos)"
        lines = []
        for t in items:
            status = t.get("status", "pending")
            content = t.get("content", "")
            lines.append(f"[{status}] {content}")
        return "\n".join(lines)


class TodoWrite(base.Tool):
    name = "todo_write"
    description = (
        "Replace the agent's working todo list. Pass an array of "
        "{content, status} items. Status must be 'pending', "
        "'in_progress', or 'completed'. Cap of 50 items. "
        "Use to plan multi-step work and to mark progress."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": list(_VALID_STATUSES),
                        },
                    },
                    "required": ["content"],
                },
            }
        },
        "required": ["todos"],
    }
    dangerous = False
    risk = "write"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        todos = args.get("todos") or []
        if not isinstance(todos, list):
            return "error: todos must be an array"
        normalized: list[dict] = []
        for i, t in enumerate(todos[:50]):
            if not isinstance(t, dict):
                continue
            content = str(t.get("content", "")).strip()
            if not content:
                continue
            status = t.get("status", "pending")
            if status not in _VALID_STATUSES:
                status = "pending"
            normalized.append({"id": i, "content": content, "status": status})
        _write(normalized)
        return f"saved {len(normalized)} todo(s) to {config.TODOS_FILE.name}"
