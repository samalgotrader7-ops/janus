"""
memory_prune.py — pure-compute card pruning (v1.18.0 Phase 8).

NO LLM calls. Deterministic decay rules:

  ACTIVE-SCOPE DROP:
    durability < 0.3 AND age >= MEMORY_PRUNE_ACTIVE_DAYS (default 21d)
    Rationale: ephemeral facts that haven't been reinforced in 3 weeks
    aren't worth keeping. They were marked "decays fast" at extraction.

  LOW-CONFIDENCE DURABLE DROP:
    durability < 0.5 AND confidence < 0.4 AND age >= 120d
    Rationale: facts the model wasn't sure about, that survived 4 months
    without confirmation, are probably noise.

  SUPERSEDED CLEANUP:
    files in cards/_superseded/ older than 30d → permanent unlink
    Rationale: supersede() is reversible; 30 days is enough audit
    window before truly deleting.

PROTECTED — NEVER DROPPED:
  durability >= MEMORY_PROTECTED_DURABILITY (default 0.7)
  These are identity-class cards. Even Phase 5 extraction can't replace
  them without explicit user action.

Active drops use ``memory_cards.supersede()`` — non-destructive. The
permanent delete only fires for cards that have already been superseded
for the configured window. Two stages = audit trail.
"""

from __future__ import annotations
import datetime as _dt
from pathlib import Path

from . import config, memory_cards, memory_index


def _parse_iso(s: str) -> _dt.datetime:
    s = (s or "").rstrip("Z")
    try:
        return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return _dt.datetime.now(_dt.timezone.utc)


def _age_days(created: str, now: _dt.datetime | None = None) -> float:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return max(0.0, (now - _parse_iso(created)).total_seconds() / 86400)


def run_once(*, now: _dt.datetime | None = None) -> dict:
    """Single pruning pass. Returns counts of drops by reason.

    ``{"removed": int, "active_drops": int, "low_conf_drops": int,
       "superseded_drops": int}``
    """
    counts = {
        "removed": 0,
        "active_drops": 0,
        "low_conf_drops": 0,
        "superseded_drops": 0,
    }
    now = now or _dt.datetime.now(_dt.timezone.utc)

    try:
        memory_index.reconcile()
    except Exception:
        pass

    rows = memory_index.list_all()
    for r in rows:
        try:
            durability = float(r["durability"])
            confidence = float(r["confidence"])
        except (TypeError, ValueError, KeyError):
            continue
        if durability >= config.MEMORY_PROTECTED_DURABILITY:
            continue
        age = _age_days(r["created"], now)

        # Active-scope drop: low durability + 21d.
        if (durability < 0.3
                and age >= config.MEMORY_PRUNE_ACTIVE_DAYS):
            memory_cards.supersede(r["id"])
            counts["active_drops"] += 1
            counts["removed"] += 1
            continue

        # Low-conf durable drop: low durability + low confidence + 120d.
        if (durability < 0.5
                and confidence < config.MEMORY_PRUNE_LOWCONF_THRESHOLD
                and age >= config.MEMORY_PRUNE_LOWCONF_DAYS):
            memory_cards.supersede(r["id"])
            counts["low_conf_drops"] += 1
            counts["removed"] += 1

    # Permanent unlink: superseded files older than the configured window.
    sup_dir = config.MEMORY_CARDS_DIR / "_superseded"
    if sup_dir.exists():
        for f in sup_dir.glob("*.md"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            age_days = (now.timestamp() - mtime) / 86400
            if age_days >= config.MEMORY_PRUNE_SUPERSEDED_DAYS:
                try:
                    f.unlink()
                    counts["superseded_drops"] += 1
                    counts["removed"] += 1
                except OSError:
                    pass

    if counts["removed"]:
        try:
            memory_index.reconcile()
        except Exception:
            pass

    return counts
