"""
skill_prune.py — pure-compute decay of stale or low-utility skills (v1.43.0).

NO LLM calls. Deterministic rules with audit windows. The hard constraint
is that this is a P4-flavored module: never silently delete user-blessed
work. Trusted (promoted) skills are NEVER auto-deleted or auto-demoted.

ACTIONS:

  QUARANTINED → TRASH:
    state == "quarantined" AND
      (age_since_created >= SKILL_PRUNE_QUARANTINE_DAYS)
    AND runs == 0
    Rationale: a draft that sat for 30+ days without ever being used is
    almost certainly noise — the user saw the offer, didn't /promote it,
    and never looked back. Move to ``~/.janus/skills/_trash/`` (non-
    destructive — restore by `mv` back to skills/).

  TRASH → PERMANENT UNLINK:
    file lives under SKILLS_DIR/_trash/ AND mtime >= SKILL_PRUNE_TRASH_DAYS
    Rationale: 30 days in trash is enough audit window before truly
    removing. Mirrors memory_prune's superseded → unlink pattern.

  STALE TRUSTED MARK:
    state in ("trusted-supervised", "trusted-auto") AND
      last_activity >= SKILL_STALE_DAYS days ago AND runs > 0
    Action: add ``stale_warning: <iso-ts>`` to frontmatter so /skills lists
    it. NEVER auto-deletes or demotes. The user re-reads + decides.

last_activity is computed as:
  max(parse(last_promoted), parse(created)) — we don't currently log a
  per-skill last-run timestamp, but a skill with non-zero runs implies
  activity since promotion. Phase 7 added a runs counter; future
  versions may add a last_used_ts field. Until then this approximation
  errs on the side of NOT warning recently-promoted skills.

PROTECTED — NEVER PRUNED:
  - Bundled skills (path under SKILLS_BUNDLED_DIR if config has one)
  - Skills with ``no-prune: true`` in frontmatter
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Optional

from . import config, skills as skills_mod


def _parse_iso(s: str) -> Optional[_dt.datetime]:
    if not s:
        return None
    s = s.rstrip("Z")
    try:
        d = _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _age_days(d: Optional[_dt.datetime], now: _dt.datetime) -> float:
    if d is None:
        return 1e9
    return max(0.0, (now - d).total_seconds() / 86400)


def _trash_dir() -> Path:
    return config.SKILLS_DIR / "_trash"


def _is_protected(skill: skills_mod.Skill) -> bool:
    fm = skill.raw_frontmatter or {}
    if fm.get("no-prune") is True or fm.get("no_prune") is True:
        return True
    # Bundled skills live outside SKILLS_DIR — they're already filtered
    # out by list_skills (which only scans SKILLS_DIR/*.md), but defend
    # in case a caller passes one in directly.
    bundled = getattr(config, "SKILLS_BUNDLED_DIR", None)
    if bundled:
        try:
            skill.path.resolve().relative_to(Path(bundled).resolve())
            return True
        except ValueError:
            return False
    return False


def _move_to_trash(skill: skills_mod.Skill) -> None:
    trash = _trash_dir()
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / skill.path.name
    # If a same-name file is already in trash, suffix with timestamp.
    if dest.exists():
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = trash / f"{skill.path.stem}.{ts}{skill.path.suffix}"
    os.replace(skill.path, dest)


def _mark_stale(skill: skills_mod.Skill, now: _dt.datetime) -> None:
    """Add stale_warning frontmatter, save atomically. Idempotent — if
    stale_warning is already present we update its timestamp."""
    fm = dict(skill.raw_frontmatter or {})
    fm["stale_warning"] = now.isoformat(timespec="seconds")
    skill.raw_frontmatter = fm
    skills_mod.save(skill)


def _clear_stale(skill: skills_mod.Skill) -> None:
    """Drop the stale_warning frontmatter, save. Called when a skill that
    previously got marked stale now shows fresh activity. We can't detect
    "fresh activity" precisely without a last_used_ts; for now this is a
    public helper that callers (e.g. record_run) MAY invoke. Default
    behavior is: don't clear automatically. Future versions wire it.
    """
    fm = dict(skill.raw_frontmatter or {})
    if "stale_warning" not in fm and "stale-warning" not in fm:
        return
    fm.pop("stale_warning", None)
    fm.pop("stale-warning", None)
    skill.raw_frontmatter = fm
    skills_mod.save(skill)


def run_once(*, now: _dt.datetime | None = None) -> dict:
    """Single skill-pruning pass. Returns counts.

    ``{"removed": int, "trashed": int, "stale_marked": int, "unlinked": int}``
    """
    counts = {
        "removed": 0,
        "trashed": 0,
        "stale_marked": 0,
        "unlinked": 0,
    }
    now = now or _dt.datetime.now(_dt.timezone.utc)

    try:
        all_skills = skills_mod.list_skills()
    except Exception:
        all_skills = []

    quarantine_days = config.SKILL_PRUNE_QUARANTINE_DAYS
    stale_days = config.SKILL_STALE_DAYS

    for s in all_skills:
        if _is_protected(s):
            continue
        created = _parse_iso(s.created)
        age = _age_days(created, now)

        if s.state == "quarantined":
            # Move to trash when old + unused.
            if age >= quarantine_days and s.runs == 0:
                try:
                    _move_to_trash(s)
                    counts["trashed"] += 1
                    counts["removed"] += 1
                except OSError:
                    pass
                continue

        if s.state in ("trusted-supervised", "trusted-auto"):
            # Mark stale (NEVER delete).
            last_promoted = _parse_iso(s.last_promoted or "") or created
            last_activity_age = _age_days(last_promoted, now)
            if last_activity_age >= stale_days and s.runs > 0:
                fm = s.raw_frontmatter or {}
                # Idempotent: skip if already marked at the same day.
                existing = fm.get("stale_warning") or fm.get("stale-warning")
                existing_dt = _parse_iso(existing) if isinstance(existing, str) else None
                if existing_dt is not None:
                    if (now - existing_dt).total_seconds() < 86400:
                        continue
                try:
                    _mark_stale(s, now)
                    counts["stale_marked"] += 1
                except OSError:
                    pass

    # Permanent unlink: trash files older than SKILL_PRUNE_TRASH_DAYS.
    trash_days = config.SKILL_PRUNE_TRASH_DAYS
    trash = _trash_dir()
    if trash.exists():
        for f in trash.glob("*.md"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            age_days = (now.timestamp() - mtime) / 86400
            if age_days >= trash_days:
                try:
                    f.unlink()
                    counts["unlinked"] += 1
                    counts["removed"] += 1
                except OSError:
                    pass

    return counts


__all__ = ["run_once"]
