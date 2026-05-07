"""
memory_refusal.py — capture user-veto events as constraint cards
(v1.24.6, item #4 from Sam's 2026-05-07 7:26 AM session).

WHY THIS EXISTS:
After Janus proposed `fs_write docs/SWARM_EXPLAINER.md` and Sam
clicked refuse, the agent immediately pivoted to inline output and
the conversation moved on — but nothing was recorded. Next session,
the same model could try to write the same file again. Vetoes are
high-signal user intent ("don't do this") that we were dropping.

The post-turn `memory.propose_diff` LLM extractor only sees the
assistant's final text. It never sees the trace, never sees what
the user blocked. This module reads the trace directly.

DESIGN — DETERMINISTIC, NO LLM:
A refusal is a discrete event: tool X with args Y returned
"refused by user: ...". We don't need an LLM to interpret that.
This module is pure compute: scan trace for refusal markers, parse
the tool/target, emit a CardProposal. No round-trip cost.

Refusal cards:
  type=constraint, origin_kind=user_refusal
  confidence=0.95  (explicit user action — high)
  importance=0.7   (governs future tool choices)
  durability=0.7   (vetoes are durable; the user changed mind once
                    they will tell us via another turn)
  conflict_resolution=append (don't supersede unrelated constraints)

Refusal cards bypass the y/N propose_diff prompt — they're
deterministic, they apply silently. The user already gave the
signal (the click that refused).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .memory_extract import CardProposal


# Tool result strings that indicate user refusal. Multiple tools emit
# variants of "refused by user:" — we match all of them.
_REFUSAL_PREFIXES = (
    "refused by user:",
    "refused by user :",
)


@dataclass(frozen=True)
class _Refusal:
    tool: str
    target: str   # path / command / url / name — whatever the tool's natural target is
    raw: str      # full result string for the card body


def _tool_target(tool: str, args: dict) -> str:
    """Best-effort: identify the user-facing target for a tool call.

    Falls back to a stringified args dict if the tool's shape is
    unfamiliar — better to record imperfect than to drop the signal.
    """
    if not isinstance(args, dict):
        return str(args)[:200]
    for key in ("path", "command", "cmd", "url", "name", "host"):
        v = args.get(key)
        if v:
            return str(v)[:200]
    return str(args)[:200]


def extract_refusals(trace: list[dict]) -> list[_Refusal]:
    """Scan a chat() trace for tool results indicating user refusal.

    Returns one _Refusal per refused tool call, in trace order.
    Empty list if the trace contains no refusals.
    """
    out: list[_Refusal] = []
    if not trace:
        return out
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        result_preview = entry.get("result_preview") or entry.get("result") or ""
        if not isinstance(result_preview, str):
            continue
        head = result_preview.lstrip().lower()
        if not any(head.startswith(p) for p in _REFUSAL_PREFIXES):
            continue
        tool = entry.get("name") or entry.get("tool") or ""
        args = entry.get("args") or {}
        target = _tool_target(tool, args)
        out.append(_Refusal(tool=tool, target=target, raw=result_preview))
    return out


# ---------- card synthesis ----------


def _slugify(s: str) -> str:
    """Compact subject-friendly slug — lowercase ASCII, dash-separated.

    We use this to make stable subject keys like
    'no-fs_write-docs-swarm_explainer-md' so a re-refusal of the same
    target writes the same subject (idempotent under conflict_resolution).
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:80] or "x"


def _scope_for(refusal: _Refusal, current_scope: str) -> str:
    """Decide card scope.

    Project-shape refusals (paths under docs/, .github/, etc., or any
    fs_write/fs_edit target) become global — the user's veto applies
    no matter which gateway re-suggests it. Per-chat refusals (e.g.
    a one-off shell command) stay at the current origin.
    """
    if refusal.tool in ("fs_write", "fs_edit", "fs_multi_edit"):
        return "global"
    return current_scope or "cli"


def _content_for(refusal: _Refusal) -> str:
    """Card body. Plain English so /memory show is readable."""
    tool = refusal.tool or "(unknown tool)"
    target = refusal.target or "(unknown target)"
    return (
        f"User refused {tool} on {target!r}. "
        f"Treat as a standing constraint: do not retry this action "
        f"without an explicit new instruction from the user."
    )


def synthesize_cards(
    refusals: Iterable[_Refusal],
    *,
    current_scope: str = "cli",
) -> list[CardProposal]:
    """Turn a sequence of refusals into CardProposal objects.

    Multiple refusals of the same (tool, target) collapse to one card
    via the deterministic subject slug. The cards layer's conflict
    resolution ('append' default) means re-refusing the same thing
    won't duplicate — it appends a fresh card with the same subject,
    and recall ranks by recency.
    """
    out: list[CardProposal] = []
    seen: set[str] = set()
    for r in refusals:
        subject = f"refuse-{_slugify(r.tool)}-{_slugify(r.target)}"
        if subject in seen:
            continue
        seen.add(subject)
        out.append(
            CardProposal(
                type="constraint",
                subject=subject,
                content=_content_for(r),
                confidence=0.95,
                importance=0.7,
                durability=0.7,
                scope=_scope_for(r, current_scope),
                conflict_resolution="append",
                origin_kind="user_refusal",
            )
        )
    return out


def cards_from_trace(
    trace: list[dict],
    *,
    current_scope: str = "cli",
) -> list[CardProposal]:
    """Convenience: scan + synthesize in one call. Returns [] when the
    trace contains no refusals (the common case). Safe to call on
    every turn — pure compute, fast on small traces."""
    refusals = extract_refusals(trace)
    if not refusals:
        return []
    return synthesize_cards(refusals, current_scope=current_scope)
