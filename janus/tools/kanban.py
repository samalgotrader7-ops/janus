"""
tools/kanban.py — model-callable tools for the Hermes-style Kanban
board (v1.42.0).

These tools let an agent inspect and grow the board from within its
own turn. A natural pattern: a `director` agent breaks a goal into
sub-tasks via `kanban_add`, then dispatches itself out of the way;
the dispatcher picks up each sub-task in turn and runs the declared
profile against it.

Tools added here:

  kanban_add     — create a new task
  kanban_list    — list tasks, optionally filtered by status/agent
  kanban_show    — full details for one task by id

All are read-or-write but the writes are SAFE (a task is just a row;
worst case a sloppy add creates noise the user can /kanban delete).
risk='read' for all so they don't hit the approver gate.

NOTE: deliberately NO `kanban_done` / `kanban_fail` here. State
transitions are owned by the dispatcher (which knows the worker id,
retry state, etc.). Letting the model close its own tasks would be a
loop hazard.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import base


class KanbanAdd(base.Tool):
    name = "kanban_add"
    description = (
        "Create a new task on the Kanban board. The dispatcher will "
        "auto-claim it when ready (parents complete) and run it via "
        "the declared agent profile. Use to delegate sub-tasks while "
        "you focus on coordination."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short imperative title (e.g. 'draft outline of X').",
            },
            "agent_profile": {
                "type": "string",
                "description": (
                    "Which agent runs this task. One of: developer, "
                    "researcher, coder, documenter, reviewer, tester, "
                    "claude. Use /agent list to see the full set."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional longer description shown to the worker "
                    "agent. Falls back to title."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Exact prompt the worker receives. If omitted, "
                    "the dispatcher synthesises one from title + "
                    "description."
                ),
            },
            "workspace": {
                "type": "string",
                "description": (
                    "Optional absolute path. Worker runs with this "
                    "as its cwd, so file-based work is scoped here."
                ),
            },
            "parent_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "IDs that must complete before this task is "
                    "claimable. Used to model dependencies."
                ),
            },
            "max_retries": {
                "type": "integer",
                "description": "Max attempts on failure (default 1).",
            },
        },
        "required": ["title", "agent_profile"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        from ..kanban import store as _ks
        title = (args.get("title") or "").strip()
        if not title:
            return "error: title is required"
        kwargs: dict[str, Any] = {
            "title": title,
            "agent_profile": (args.get("agent_profile") or "developer").strip(),
            "description": (args.get("description") or "").strip(),
            "prompt": (args.get("prompt") or "").strip(),
            "workspace": (args.get("workspace") or "").strip(),
            "parent_ids": list(args.get("parent_ids") or []),
            "max_retries": int(args.get("max_retries") or 1),
        }
        try:
            t = _ks.create_task(**kwargs)
        except (ValueError, TypeError) as e:
            return f"error: {e}"
        deps = f" (deps: {t.parent_ids})" if t.parent_ids else ""
        return (
            f"created kanban task #{t.id} [{t.status}] "
            f"@{t.agent_profile}: {t.title}{deps}"
        )


class KanbanList(base.Tool):
    name = "kanban_list"
    description = (
        "List tasks on the Kanban board. Optional filters by status "
        "or agent_profile. Returns a JSON array of task summaries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "Filter to one status: backlog, ready, "
                    "in_progress, completed, failed, blocked."
                ),
            },
            "agent_profile": {
                "type": "string",
                "description": "Filter to tasks for this agent only.",
            },
        },
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        from ..kanban import store as _ks
        status = (args.get("status") or "").strip() or None
        agent = (args.get("agent_profile") or "").strip() or None
        tasks = _ks.list_tasks(status=status, agent_profile=agent)
        summaries = [
            {
                "id": t.id,
                "status": t.status,
                "agent": t.agent_profile,
                "title": t.title,
                "parents": t.parent_ids,
                "workspace": t.workspace,
            }
            for t in tasks
        ]
        return json.dumps(summaries, ensure_ascii=False, indent=2)


class KanbanShow(base.Tool):
    name = "kanban_show"
    description = (
        "Full details for one Kanban task (description, prompt, "
        "output, error). Use after kanban_list to inspect a specific "
        "entry, or to read a completed sub-task's output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {
                "type": "integer",
                "description": "Task id (from kanban_list).",
            },
        },
        "required": ["id"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        from ..kanban import store as _ks
        try:
            tid = int(args["id"])
        except (KeyError, ValueError, TypeError):
            return "error: id must be an integer"
        t = _ks.get_task(tid)
        if not t:
            return f"error: task #{tid} not found"
        return json.dumps(t.to_dict(), ensure_ascii=False, indent=2)
