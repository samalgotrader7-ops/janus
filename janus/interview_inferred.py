"""
interview_inferred.py — heuristic suggestion queue (v1.19.0 Phase 7).

Idea: when the user mentions something whose category has no cards
yet, Janus offers — once, gently — to record it via a single
interview question. This catches what the cold-start interview
missed: things people don't enumerate on demand but mention naturally
("I'm working on a forex bot" → no project cards yet → offer to
record as a project).

Pure compute (no LLM). Keyword catalog per category; substring
matching against the user's input + assistant's output. Deduped
against the v1.18 cards layer so we don't offer to record what's
already covered. 30-day cooldown per declined category so the agent
isn't naggy.

ONE OFFER PER TURN MAX. Multiple matches in one turn → pick the
highest-priority and queue just that one. The next turn's assistant
reply gets a single prepended offer:

    💡 I noticed you mentioned a project. Want me to record it as a
       project card? (reply 'yes' / 'no' / 'mute project')
"""

from __future__ import annotations
import datetime as _dt
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config, interviews, memory_index


# Cooldown after the user declines an inferred offer (in days).
INFERRED_COOLDOWN_DAYS = int(
    os.getenv("JANUS_INTERVIEW_INFERRED_COOLDOWN_DAYS", "30")
)


# Keyword catalog per category — small and obvious. Each entry is a
# lowercase substring; we substring-match against (user_input + output)
# to detect the category.
#
# Order matters within a category — first match wins for the hint text.
# Across categories, priority is the SUPPORTED_CATEGORIES order (so
# identity beats preference beats goal etc).
KEYWORDS: dict[str, list[str]] = {
    "identity": [
        "i'm a ", "i am a ", "i work as ", "my name is ",
        "i go by ", "call me ",
    ],
    "preference": [
        "i prefer ", "i like ", "i dislike ", "i hate ",
        "i love ", "always use ", "never use ",
    ],
    "goal": [
        "i'm trying to ", "my goal is ", "by end of ",
        "trying to ship ", "want to finish ", "deadline",
    ],
    "project": [
        "i'm working on ", "my project ", "i'm building ",
        "currently building ", "shipping ", "side project",
    ],
    "habit": [
        "every morning", "every day", "daily ", "weekly ",
        "i always ", "i usually ", "every monday", "every friday",
    ],
    "decision": [
        "i chose ", "i picked ", "i decided ", "switched to ",
        "went with ", "moving away from ",
    ],
    "constraint": [
        "i can't ", "limited to ", "budget", "compliance",
        "must follow ", "not allowed to ",
    ],
    "relationship": [
        "my team", "my boss", "my colleague", "my client",
        "my partner", "my coworker", "my manager",
    ],
}


@dataclass
class Hint:
    category: str
    matched_phrase: str        # the keyword that triggered
    detected_at: str = ""      # ISO timestamp


# ---------- Scan (pure compute) ----------


def scan(text: str, *,
         excluded_categories: Optional[set[str]] = None) -> list[Hint]:
    """Return a list of category hints found in ``text``.

    Walks SUPPORTED_CATEGORIES in priority order; ONE hint per category
    (the first matching keyword for that category). Pass
    ``excluded_categories`` to skip categories the caller already knows
    are covered (cards exist).
    """
    if not text:
        return []
    excluded = excluded_categories or set()
    haystack = text.lower()
    out: list[Hint] = []
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for category in interviews.SUPPORTED_CATEGORIES:
        if category in excluded:
            continue
        for kw in KEYWORDS.get(category, []):
            if kw in haystack:
                out.append(Hint(
                    category=category,
                    matched_phrase=kw.strip(),
                    detected_at=now,
                ))
                break
    return out


def covered_categories() -> set[str]:
    """Categories that already have at least one card. Smart-skip for
    inferred suggestions — don't offer to record a project when project
    cards already exist."""
    try:
        memory_index.reconcile()
        rows = memory_index.list_all()
    except Exception:
        return set()
    return {r["type"] for r in rows}


# ---------- State persistence ----------


def _state_dir() -> Path:
    return interviews.interviews_dir() / "_inferred"


def _safe_chat(chat_id: str) -> str:
    s = str(chat_id or "default")
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in s) or "default"


def _state_path(gateway: str, chat_id: str) -> Path:
    return _state_dir() / f"{gateway}__{_safe_chat(chat_id)}.json"


def _load(gateway: str, chat_id: str) -> dict:
    p = _state_path(gateway, chat_id)
    if not p.exists():
        return {"pending": [], "cooldowns": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pending": [], "cooldowns": {}}
    if not isinstance(data, dict):
        return {"pending": [], "cooldowns": {}}
    data.setdefault("pending", [])
    data.setdefault("cooldowns", {})
    return data


def _save(gateway: str, chat_id: str, data: dict) -> None:
    p = _state_path(gateway, chat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_in_cooldown(data: dict, category: str,
                    *, now: Optional[_dt.datetime] = None) -> bool:
    """True when a category was declined within the cooldown window."""
    when_str = (data.get("cooldowns") or {}).get(category)
    if not when_str:
        return False
    try:
        when = _dt.datetime.fromisoformat(
            when_str.rstrip("Z")
        ).replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return False
    now = now or _dt.datetime.now(_dt.timezone.utc)
    elapsed = (now - when).total_seconds() / 86400
    return elapsed < INFERRED_COOLDOWN_DAYS


# ---------- Public API ----------


def scan_and_queue(
    request: str,
    output: str,
    *,
    gateway: str,
    chat_id: str,
    now: Optional[_dt.datetime] = None,
) -> Optional[Hint]:
    """Hook called by ``memory.propose_diff`` post-extraction.

    Scans text for category hints, filters out already-covered
    categories AND categories in cooldown. Queues at most ONE hint per
    turn. Returns the queued hint (or None) for testability.
    """
    excluded = covered_categories()
    state = _load(gateway, chat_id)
    cooldown_filter = {
        cat for cat in interviews.SUPPORTED_CATEGORIES
        if _is_in_cooldown(state, cat, now=now)
    }
    excluded |= cooldown_filter

    # Skip if we already have a pending hint queued (don't pile up).
    if state.get("pending"):
        return None

    text = (request or "") + "\n" + (output or "")
    hints = scan(text, excluded_categories=excluded)
    if not hints:
        return None
    # Take the highest-priority single hint.
    chosen = hints[0]
    state["pending"] = [{
        "category": chosen.category,
        "matched_phrase": chosen.matched_phrase,
        "detected_at": chosen.detected_at,
    }]
    _save(gateway, chat_id, state)
    return chosen


def pop_pending(gateway: str, chat_id: str) -> Optional[Hint]:
    """Read + remove the oldest pending hint. Returns None if none."""
    state = _load(gateway, chat_id)
    pending = state.get("pending") or []
    if not pending:
        return None
    raw = pending.pop(0)
    state["pending"] = pending
    _save(gateway, chat_id, state)
    return Hint(
        category=str(raw.get("category") or ""),
        matched_phrase=str(raw.get("matched_phrase") or ""),
        detected_at=str(raw.get("detected_at") or ""),
    )


def peek_pending(gateway: str, chat_id: str) -> Optional[Hint]:
    """Look at the oldest pending hint WITHOUT removing it."""
    state = _load(gateway, chat_id)
    pending = state.get("pending") or []
    if not pending:
        return None
    raw = pending[0]
    return Hint(
        category=str(raw.get("category") or ""),
        matched_phrase=str(raw.get("matched_phrase") or ""),
        detected_at=str(raw.get("detected_at") or ""),
    )


def mark_declined(gateway: str, chat_id: str, category: str,
                  *, now: Optional[_dt.datetime] = None) -> None:
    """30-day cooldown after the user declines a category offer."""
    state = _load(gateway, chat_id)
    state.setdefault("cooldowns", {})[category] = (
        now or _dt.datetime.now(_dt.timezone.utc)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Drop any pending hint for this category — it was just declined.
    state["pending"] = [
        p for p in (state.get("pending") or [])
        if p.get("category") != category
    ]
    _save(gateway, chat_id, state)


def render_offer(hint: Hint) -> str:
    """Plain-text offer the gateway can prepend to its assistant reply."""
    return (
        f"💡 I noticed you mentioned a {hint.category} "
        f"(\"{hint.matched_phrase}\"). Want me to record it as a "
        f"{hint.category} card? Reply 'yes' / 'no' / "
        f"'mute {hint.category}'."
    )
