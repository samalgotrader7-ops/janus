"""
statusline.py — render the persistent status string (Phase 14).

WHY:
Claude Code shows a status line at the bottom of the terminal — model,
turn, tokens, mode. Our basic CLI prints it just before each prompt
(simpler than ANSI cursor positioning, works in every terminal). The
rich CLI uses `rich.live.Live` to render it as an actual bottom bar.

This module is purely a string builder; it does not touch the terminal
itself. The caller decides where to render.
"""

from __future__ import annotations
from dataclasses import dataclass

from . import config, cost


@dataclass
class StatusInputs:
    model: str
    turn: int                # 1-indexed; 0 means "no turn yet"
    plan_on: bool = False
    parallel_on: bool = False
    skill: str | None = None
    permission_mode: str = ""   # "manual" / "auto" / "dry-run"
    verbose: bool = False
    conv_id: str | None = None
    conv_turns: int = 0


def render(s: StatusInputs) -> str:
    """Compact one-liner. Use ' · ' as the separator so eye-saccade is
    consistent with the banner."""
    parts: list[str] = []
    parts.append(f"model: {s.model}")
    if s.turn > 0:
        parts.append(f"turn {s.turn}")
    cs = cost.session_stats()
    if cs.prompt_tokens or cs.completion_tokens:
        if cs.usd > 0:
            parts.append(
                f"{cs.prompt_tokens + cs.completion_tokens:,} tok · ${cs.usd:.3f}"
            )
        else:
            parts.append(f"{cs.prompt_tokens + cs.completion_tokens:,} tok")
    if s.skill:
        parts.append(f"skill: {s.skill}")
    if s.plan_on:
        parts.append("plan")
    if s.parallel_on:
        parts.append("parallel")
    if s.verbose:
        parts.append("verbose")
    if s.permission_mode and s.permission_mode != "manual":
        parts.append(f"approval: {s.permission_mode}")
    if s.conv_turns:
        parts.append(f"conv: {s.conv_turns} turns")
    return "  " + " · ".join(parts)
