"""
task_render.py — Rich rendering for the agent's todo list (v1.25.3).

When the model calls ``todo_write`` or ``todo_read``, cli_rich displays
the current list as a panel: status icons, colors, and strikethrough on
completed items. Same shape as Claude Code's task checklist.

The point isn't a sticky pane (Janus is line-oriented like Claude Code,
not Textual-shaped). Each tool call renders the latest state inline,
which is what the model and user actually want to see — the audit
trail stays in the conversation log.

Rendering is decoupled from cli_rich: this module returns Rich
renderables given a list-of-dicts. cli_rich just prints them. Other
surfaces (web, telegram) can adopt the same renderer if they want.

DESIGN — STATUS-FIRST LAYOUT:
- Each line: ``<icon> <content>``
- Icons (BMP, render on any UTF-8 terminal — survive the v1.24.6
  emoji-safe filter):
    ○  pending
    ▶  in_progress
    ✓  completed
- Colors:
    pending     → dim
    in_progress → bold cyan
    completed   → dim + strikethrough
- Header: "todos · {n_done}/{n_total}" so the user sees progress at
  a glance even when the list scrolls past.
- Empty list: show "(no todos)" with a hint to use todo_write.
"""

from __future__ import annotations

from typing import Any


# Status → (icon, rich_style) — keep BMP only so emoji_safe_text is a no-op.
_STATUS_ICON: dict[str, str] = {
    "pending":     "○",
    "in_progress": "▶",
    "completed":   "✓",
}

_STATUS_STYLE: dict[str, str] = {
    "pending":     "dim",
    "in_progress": "bold cyan",
    "completed":   "dim strike",
}


def _status_of(item: Any) -> str:
    if not isinstance(item, dict):
        return "pending"
    s = str(item.get("status", "pending")).strip().lower()
    return s if s in _STATUS_ICON else "pending"


def _content_of(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("content", "")).rstrip()


def render_plain(todos: list) -> str:
    """ASCII-only rendering — used by tests and headless surfaces.

    Each line: ``<icon> <content>``. Empty list gets a hint string.
    """
    if not todos:
        return "(no todos — use todo_write to plan multi-step work)"
    lines = []
    n_total = 0
    n_done = 0
    for it in todos:
        content = _content_of(it)
        if not content:
            continue
        n_total += 1
        status = _status_of(it)
        if status == "completed":
            n_done += 1
        icon = _STATUS_ICON[status]
        lines.append(f"{icon} {content}")
    header = f"todos · {n_done}/{n_total} done"
    return header + "\n" + "\n".join(lines) if lines else "(no todos)"


def render_rich_panel(todos: list):
    """Build a Rich Panel for the current todos. Caller prints it via
    Console.print. Returns None if rich isn't importable so callers
    can fall back to plain text.

    The panel's border style and title color match Janus's brand
    magenta so it visually anchors as Janus output, not user output.
    """
    try:
        from rich.panel import Panel
        from rich.text import Text
        from rich.console import Group
    except ImportError:
        return None
    if not todos:
        body = Text(
            "(no todos — use todo_write to plan multi-step work)",
            style="dim italic",
        )
        return Panel(body, title="todos", border_style="magenta", expand=False)

    n_total = sum(1 for t in todos if _content_of(t))
    n_done = sum(
        1 for t in todos
        if _content_of(t) and _status_of(t) == "completed"
    )
    n_active = sum(
        1 for t in todos
        if _content_of(t) and _status_of(t) == "in_progress"
    )

    rows: list[Text] = []
    for it in todos:
        content = _content_of(it)
        if not content:
            continue
        status = _status_of(it)
        icon = _STATUS_ICON[status]
        style = _STATUS_STYLE[status]
        line = Text()
        line.append(f"{icon}  ", style=style)
        line.append(content, style=style)
        rows.append(line)

    # Header line: progress + active marker if any task is mid-flight.
    title = f"todos · {n_done}/{n_total} done"
    if n_active:
        title += f" · {n_active} in progress"

    return Panel(
        Group(*rows) if rows else Text("(empty)", style="dim"),
        title=title,
        border_style="magenta",
        expand=False,
    )


def parse_todos_from_disk(todos_path) -> list:
    """Read the todo list directly from ~/.janus/todos.json.

    Used by the cli_rich renderer to ALWAYS show the current on-disk
    state after a todo_write tool call — even if the tool's result
    string was truncated for display. Returns [] on any failure.
    """
    import json
    from pathlib import Path
    p = Path(todos_path) if not isinstance(todos_path, Path) else todos_path
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []
