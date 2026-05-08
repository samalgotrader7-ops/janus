"""
plan_render.py — structured plan-review rendering (v1.27.2).

Pre-v1.27.2, ExitPlanMode plans were rendered as a generic yellow
``Panel`` containing the raw plan markdown. The user saw the same
shape they'd see for any approval prompt — no visual cue that this
was a "review my plan, can I execute?" moment.

This module provides:

  * ``parse_plan(plan_text)`` — pure-compute parser that extracts
    numbered/bulleted steps, file:line references, and any model-
    supplied tool-count estimate from the plan body.

  * ``render_rich_panel(parsed, plan_text, *, mode)`` — returns a
    Rich Panel rendering with title "Plan Review", a header line
    showing step + file + tool-count metrics, and the full plan
    body as Markdown (so ``## Steps`` etc. render natively).

  * ``render_plain(parsed)`` — ASCII fallback for surfaces without
    Rich (basic CLI, headless, tests).

DESIGN CHOICES:

  * **Pure-compute parser, no LLM.** The model already produced the
    plan; re-asking it to "structure" the plan would burn tokens for
    no gain. A regex pass on the markdown is sufficient.

  * **Forgiving regex.** Models phrase plans differently:
    "1. step" / "1) step" / "- step" / "* step". All accepted.

  * **File:line preserved.** When the plan mentions ``foo.py:42``
    the metric counts it; the panel body keeps the original
    Markdown so links + colors aren't lost.

  * **Optional Rich import.** This module imports rich.panel /
    rich.markdown lazily so headless tests (no rich installed)
    still get the plain renderer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------- Regex patterns ----------

# Numbered: "1. step" / "1) step" / "10. step" — at start of a line.
_NUMBERED_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+(.+?)\s*$", re.MULTILINE)

# Bulleted: "- step" / "* step" / "+ step" — at start of a line.
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$", re.MULTILINE)

# File:line references using the Janus convention: ``path/to/x.py:42``
# Extensions must start with a LETTER (not digit) — otherwise version
# strings like ``v1.27.2`` would parse as path=v1.27 ext=2.
# Allowed extensions: py / json / md / .gitignore-shape (letter-first).
_FILE_LINE_RE = re.compile(
    r"`?\b([A-Za-z0-9_./\\-]+\.[A-Za-z][A-Za-z0-9]{0,7})(?::(\d+))?`?"
)

# Backtick-quoted filenames: `foo.py`, `tests/test_x.py`. Same
# letter-first-extension rule.
_BACKTICK_FILE_RE = re.compile(r"`([A-Za-z0-9_./\\-]+\.[A-Za-z][A-Za-z0-9]{0,7})`")

# Tool-count estimate: "approximately 8 tool calls", "~5 calls",
# "estimated 12 calls". Loose; we accept any number adjacent to "tool".
_TOOL_COUNT_RE = re.compile(
    r"(?:approximately|approx|estimated|estimate|about|around|~)?\s*"
    r"(\d{1,3})\s+(?:tool|tool calls?|tool-calls?|calls?)\b",
    re.IGNORECASE,
)


# ---------- Data class ----------


@dataclass
class ParsedPlan:
    """Structured view of a plan body.

    ``raw_text`` keeps the original Markdown so renderers can show
    the full plan; the metrics support a header-line summary.
    """
    raw_text: str
    steps: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    estimated_tool_calls: Optional[int] = None

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def file_count(self) -> int:
        return len(self.files)


# ---------- Parser ----------


def parse_plan(plan_text: str) -> ParsedPlan:
    """Extract structured metrics from plan markdown.

    Numbered steps win over bulleted. If a plan has both, only the
    numbered list is counted as "steps" — bullets are usually
    sub-points within a step, not separate steps.

    Files are deduplicated, order-preserving (a path mentioned
    multiple times shows up once in the list).
    """
    plan_text = plan_text or ""

    # Steps — numbered first, fallback to bulleted.
    steps: list[str] = []
    numbered = _NUMBERED_RE.findall(plan_text)
    if numbered:
        # Sort by the leading number so out-of-order plans display
        # in step-order. (Some models emit "1. ... 3. ... 2. ..." by
        # accident.)
        try:
            numbered_sorted = sorted(numbered, key=lambda m: int(m[0]))
            steps = [m[1].strip() for m in numbered_sorted]
        except (TypeError, ValueError):
            steps = [m[1].strip() for m in numbered]
    else:
        # Pure bulleted plan
        bullets = _BULLET_RE.findall(plan_text)
        steps = [b.strip() for b in bullets]

    # Files — file.ext or path/file.ext, with optional :line suffix.
    files: list[str] = []
    seen_files: set[str] = set()

    # First pass: backtick-quoted (highest signal, preserves the
    # path-as-code intent).
    for m in _BACKTICK_FILE_RE.finditer(plan_text):
        f = m.group(1)
        if f and f not in seen_files:
            seen_files.add(f)
            files.append(f)

    # Second pass: bare path:line references.
    for m in _FILE_LINE_RE.finditer(plan_text):
        path = m.group(1)
        line = m.group(2)
        # Skip if it looks like a number-only token, or a version-shaped
        # word (we don't want "v1.27.2" matching).
        if not path or path[0].isdigit():
            # Versions like "1.27" — only skip if no path separator
            # AND no underscore (real paths often have one).
            if "/" not in path and "\\" not in path and "_" not in path:
                continue
        # Refuse trailing-dot shapes like "foo." with empty extension —
        # the regex requires at least 1-char extension so this is rare.
        full = f"{path}:{line}" if line else path
        if full not in seen_files:
            seen_files.add(full)
            files.append(full)

    # Tool count estimate — last match wins (model may revise).
    estimated: Optional[int] = None
    matches = _TOOL_COUNT_RE.findall(plan_text)
    if matches:
        try:
            estimated = int(matches[-1])
        except (TypeError, ValueError):
            estimated = None

    return ParsedPlan(
        raw_text=plan_text,
        steps=steps,
        files=files,
        estimated_tool_calls=estimated,
    )


# ---------- Rendering ----------


def _format_metric_line(parsed: ParsedPlan) -> str:
    bits: list[str] = []
    if parsed.step_count:
        word = "step" if parsed.step_count == 1 else "steps"
        bits.append(f"{parsed.step_count} {word}")
    if parsed.file_count:
        word = "file" if parsed.file_count == 1 else "files"
        bits.append(f"{parsed.file_count} {word}")
    if parsed.estimated_tool_calls is not None:
        word = "call" if parsed.estimated_tool_calls == 1 else "calls"
        bits.append(f"~{parsed.estimated_tool_calls} tool {word}")
    return " · ".join(bits) if bits else "(no metrics extracted)"


def render_plain(parsed: ParsedPlan) -> str:
    """ASCII rendering for surfaces without Rich.

    Format:
        Plan Review · 5 steps · 3 files · ~12 tool calls
        ============================================================
        <full plan body>
    """
    header = f"Plan Review · {_format_metric_line(parsed)}"
    sep = "=" * min(len(header), 60)
    return f"{header}\n{sep}\n{parsed.raw_text}".rstrip()


def render_rich_panel(parsed: ParsedPlan, plan_text: str, *, mode: str = "plan"):
    """Return a Rich renderable (Panel + Markdown) or None if Rich unavailable.

    The caller should fall back to ``render_plain`` if this returns
    None — keeps the approver path resilient on minimal installs.
    """
    try:
        from rich.panel import Panel
        from rich.markdown import Markdown
        from rich.console import Group
        from rich.text import Text
    except ImportError:
        return None

    metric = _format_metric_line(parsed)
    subtitle = f"[dim]mode={mode}[/]"
    title = f"[bold cyan]Plan Review[/] · [yellow]{metric}[/]"

    # Body — render the plan as Markdown so headers + lists look right.
    # We keep the metrics line ABOVE the markdown body for at-a-glance
    # scanability.
    metric_text = Text.from_markup(
        f"[bold]Steps:[/] {parsed.step_count}  "
        f"[bold]Files:[/] {parsed.file_count}"
        + (
            f"  [bold]Est. tool calls:[/] ~{parsed.estimated_tool_calls}"
            if parsed.estimated_tool_calls is not None
            else ""
        )
    )

    file_list_text: Optional[Text] = None
    if parsed.files:
        # Cap at 8 files in the header to keep the panel compact;
        # the full list is in the markdown body anyway.
        shown = parsed.files[:8]
        more = (
            f"  [dim](+{len(parsed.files) - 8} more)[/]"
            if len(parsed.files) > 8
            else ""
        )
        file_list_text = Text.from_markup(
            "[dim]Files to touch:[/] "
            + ", ".join(f"[cyan]{f}[/]" for f in shown)
            + more
        )

    body_parts = [metric_text]
    if file_list_text is not None:
        body_parts.append(file_list_text)
    body_parts.append(Text(""))  # blank line
    body_parts.append(Markdown(plan_text or ""))

    return Panel(
        Group(*body_parts),
        title=title,
        subtitle=subtitle,
        border_style="cyan",
        padding=(1, 2),
    )


# ---------- Detection helper ----------


def is_plan_action(action_label: str) -> bool:
    """True if the approver call looks like an ExitPlanMode invocation.

    The approver receives ``action_label`` from the tool — for
    ExitPlanMode that's literally ``"exit_plan_mode"``.
    """
    if not action_label:
        return False
    return "exit_plan_mode" in action_label.lower()


# ---------- Cross-surface renderers (v1.30.0) ----------

# Telegram caps a message body at 4096 chars. Leave headroom for the
# header line + a possible trailing "(plan body truncated)" note.
TELEGRAM_BODY_CAP = 3600


def render_telegram_text(
    parsed: ParsedPlan, plan_text: str, *, mode: str = "plan",
) -> str:
    """Plain-text plan-review body for Telegram.

    Returns a string the gateway sends with ``parse_mode=None`` (plain
    text). v1.31.7 — switched from Markdown to plain text after Sam's
    VPS validation found that model-generated plan bodies often
    contain markdown that Telegram's parse_mode="Markdown" can't
    parse (`**bold**`, identifiers like `JANUS_COST_BUDGET_STRICT`
    where adjacent underscores trip italic detection, etc.). The
    silent send_message failure left the approver hung on the
    approval future for 30 minutes.

    The new shape uses unicode for visual structure (emoji + box-
    drawing separator) instead of markdown formatting. Headers are
    plain text. The body shows literal content — no risk of parse
    failure. ``parse_mode=None`` is the only safe choice for
    unpredictable model output.

    Trade-off: lose nice italic/bold styling in the metric line.
    Worth it for guaranteed delivery.
    """
    metric = _format_metric_line(parsed)
    header_line = f"📋 PLAN REVIEW · {metric} · mode={mode}"
    separator = "─" * 40

    body = (plan_text or "").strip()
    truncated = False
    if len(body) > TELEGRAM_BODY_CAP:
        body = body[:TELEGRAM_BODY_CAP].rstrip()
        truncated = True

    parts = [header_line, separator, body]
    if truncated:
        parts.append("")
        parts.append("(plan body truncated — full plan is in the audit log)")
    return "\n".join(parts)


def build_web_payload(
    parsed: ParsedPlan, plan_text: str, *, mode: str = "plan",
) -> dict:
    """Structured payload included on the ``approval_pending`` SSE event.

    The web client renders a dedicated plan-review modal when this
    payload is present (vs. the generic approval modal). All values are
    JSON-serializable primitives so the wire format stays simple.

    Files are capped at 16 in the payload — the full list lives in
    ``raw_text`` if a renderer wants to recover it. 16 fits a single
    panel of file chips without horizontal overflow.
    """
    files = parsed.files[:16]
    return {
        "mode": mode,
        "metric_line": _format_metric_line(parsed),
        "step_count": parsed.step_count,
        "file_count": parsed.file_count,
        "estimated_tool_calls": parsed.estimated_tool_calls,
        "steps": list(parsed.steps),
        "files": list(files),
        "files_truncated": parsed.file_count > len(files),
        "body_md": plan_text or "",
    }


__all__ = [
    "ParsedPlan",
    "parse_plan",
    "render_plain",
    "render_rich_panel",
    "is_plan_action",
    "render_telegram_text",
    "build_web_payload",
    "TELEGRAM_BODY_CAP",
]
