"""
event_render.py — pure-compute event-to-display mapping (v1.35.4,
Phase 9.1).

WHY:
The SSE event stream surfaces structured events
(tool_call / tool_result / skill_loaded / memory_update /
subagent_step / etc.) as they happen. Pre-v1.35.4 the web frontend
only rendered the FINAL assistant message; events flowed through
but rendered post-turn. cli_rich already shows them live; this
module ships the shared mapping so the web (and any future
surface) can render the same way.

DESIGN — PURE FUNCTION:
render_event(event) returns (kind, line) — a tuple the caller
formats however suits the surface. No DOM / Rich / Markdown
coupling here; per-surface renderers consume the tuple.

EVENT KINDS HANDLED (matches janus.app.EVENT_TYPES):
  user_turn, assistant_text, tool_call, tool_result,
  skill_loaded, memory_update, hook_fired, memory_recall,
  thinking, subagent_start, subagent_step, subagent_end,
  budget_alert, verification_result, plan_review,
  approval_pending, mode_change, keepalive

Anything else returns ('unknown', '...'). Frontend can choose to
render or hide unknowns.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderedEvent:
    """One event mapped to a display-friendly tuple."""

    kind: str       # event family — 'tool', 'skill', 'memory', 'subagent', 'system'
    glyph: str      # short marker, e.g. '🔧', '📚', '🧠', '✓'
    line: str       # primary single-line display text
    detail: str = ""  # optional second-line / secondary detail


# Glyph table — kept in sync with gateways/_common.INDICATOR_GLYPHS
# but defined here so this module has zero gateway dependency.
_GLYPHS: dict[str, str] = {
    "tool_call": "🔧",
    "tool_result": "✓",
    "tool_result_err": "✗",
    "skill_loaded": "📚",
    "memory_update": "🧠",
    "memory_recall": "🧠",
    "thinking": "⚡",
    "subagent_start": "▸",
    "subagent_step": "·",
    "subagent_end": "◂",
    "hook_fired": "⚙",
    "budget_alert": "💰",
    "verification_result": "🧪",
    "plan_review": "📋",
    "approval_pending": "❓",
    "mode_change": "↻",
}


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def render_event(event: dict | None) -> RenderedEvent:
    """Map one structured event to a renderable tuple. Tolerant of
    missing fields — never raises on malformed input."""
    if not isinstance(event, dict):
        return RenderedEvent(kind="unknown", glyph="?", line="(empty event)")

    et = str(event.get("type", "") or event.get("kind", ""))
    payload = event.get("payload") or event

    if et == "tool_call":
        name = str(payload.get("tool", "") or payload.get("name", "") or "?")
        args = payload.get("args") or {}
        if isinstance(args, dict):
            args_str = ", ".join(f"{k}={_truncate(str(v), 30)}" for k, v in args.items())
        else:
            args_str = str(args)
        return RenderedEvent(
            kind="tool", glyph=_GLYPHS["tool_call"],
            line=f"tool: {name}",
            detail=_truncate(args_str, 200),
        )

    if et == "tool_result":
        name = str(payload.get("tool", "") or "?")
        preview = _truncate(str(payload.get("result_preview", "")), 200)
        is_err = "error" in preview.lower()[:40]
        glyph = _GLYPHS["tool_result_err"] if is_err else _GLYPHS["tool_result"]
        return RenderedEvent(
            kind="tool", glyph=glyph,
            line=f"{name}",
            detail=preview,
        )

    if et == "skill_loaded":
        name = str(payload.get("name", "?"))
        state = str(payload.get("state", ""))
        line = f"skill: {name}"
        if state:
            line += f"  ({state})"
        return RenderedEvent(kind="skill", glyph=_GLYPHS["skill_loaded"], line=line)

    if et == "memory_update":
        n = payload.get("op_count", 0)
        summary = _truncate(str(payload.get("summary", "")), 120)
        line = f"memory: {n} update(s)"
        if summary:
            line += f" — {summary}"
        return RenderedEvent(kind="memory", glyph=_GLYPHS["memory_update"], line=line)

    if et == "memory_recall":
        n = payload.get("count", 0)
        return RenderedEvent(
            kind="memory", glyph=_GLYPHS["memory_recall"],
            line=f"memory recall: {n} card(s)",
        )

    if et == "thinking":
        note = _truncate(str(payload.get("note", "")), 200)
        return RenderedEvent(kind="thinking", glyph=_GLYPHS["thinking"], line=note or "...")

    if et == "subagent_start":
        desc = _truncate(str(payload.get("description", "")), 80)
        return RenderedEvent(
            kind="subagent", glyph=_GLYPHS["subagent_start"],
            line=f"subagent: {desc}",
        )

    if et == "subagent_step":
        return RenderedEvent(
            kind="subagent", glyph=_GLYPHS["subagent_step"],
            line=_truncate(str(payload.get("note", "")), 200) or "...",
        )

    if et == "subagent_end":
        ok = bool(payload.get("success", True))
        line = "subagent: done" if ok else "subagent: failed"
        return RenderedEvent(kind="subagent", glyph=_GLYPHS["subagent_end"], line=line)

    if et == "hook_fired":
        name = str(payload.get("name", "?"))
        return RenderedEvent(kind="system", glyph=_GLYPHS["hook_fired"], line=f"hook: {name}")

    if et == "budget_alert":
        pct = payload.get("percent", 0)
        return RenderedEvent(
            kind="system", glyph=_GLYPHS["budget_alert"],
            line=f"budget: {pct}% used",
        )

    if et == "verification_result":
        ok = bool(payload.get("passed", False))
        line = "verification: passed" if ok else "verification: failed"
        return RenderedEvent(kind="system", glyph=_GLYPHS["verification_result"], line=line)

    if et == "plan_review":
        return RenderedEvent(kind="system", glyph=_GLYPHS["plan_review"], line="plan review")

    if et == "mode_change":
        old = payload.get("from_mode") or payload.get("from")
        new = payload.get("to_mode") or payload.get("to")
        return RenderedEvent(
            kind="system", glyph=_GLYPHS["mode_change"],
            line=f"mode: {old} → {new}",
        )

    if et == "keepalive":
        return RenderedEvent(kind="system", glyph="·", line="(keepalive)")

    return RenderedEvent(kind="unknown", glyph="?", line=f"({et}) {str(payload)[:120]}")
