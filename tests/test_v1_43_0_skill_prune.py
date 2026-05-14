"""Tests for v1.43.0 — skill_prune deterministic decay rules.

Pinned invariants:
  * Quarantined skills with runs==0 and age >= QUARANTINE_DAYS get
    moved to ``SKILLS_DIR/_trash/`` (reversible).
  * Trash files older than TRASH_DAYS get permanent unlink.
  * Trusted (promoted) skills are NEVER auto-deleted or auto-demoted.
  * Trusted skills inactive for >= STALE_DAYS get a stale_warning
    frontmatter flag, idempotent within a day.
  * Frontmatter with ``no-prune: true`` is protected.
  * Quarantined skills with non-zero runs (= someone used them) are
    NOT trashed even when aged — usage is a signal of value even
    without /promote.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from janus import config, skills as skills_mod, skill_prune
from janus.tools.capabilities import CapabilitySet


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    skills_dir = home / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(config, "SKILL_PRUNE_QUARANTINE_DAYS", 30)
    monkeypatch.setattr(config, "SKILL_PRUNE_TRASH_DAYS", 30)
    monkeypatch.setattr(config, "SKILL_STALE_DAYS", 60)
    yield home


def _make_skill(
    home: Path,
    *,
    name: str,
    state: str = "quarantined",
    created_days_ago: int = 0,
    runs: int = 0,
    last_promoted_days_ago: int | None = None,
    extra_fm: dict | None = None,
) -> Path:
    now = _dt.datetime.now(_dt.timezone.utc)
    created = (now - _dt.timedelta(days=created_days_ago)).isoformat(
        timespec="seconds"
    )
    last_promoted = None
    if last_promoted_days_ago is not None:
        last_promoted = (
            now - _dt.timedelta(days=last_promoted_days_ago)
        ).isoformat(timespec="seconds")

    skill = skills_mod.Skill(
        name=name,
        description=f"test skill {name}",
        state=state,
        capabilities=CapabilitySet.from_dict({}),
        body="(test body)",
        path=config.SKILLS_DIR / f"{name}.md",
        raw_frontmatter=dict(extra_fm or {}),
        created=created,
        last_promoted=last_promoted,
        runs=runs,
        success=0,
        fail=0,
    )
    skills_mod.save(skill)
    return skill.path


# ============================================================
# Quarantined → trash
# ============================================================


class TestQuarantinedToTrash:
    def test_old_unused_quarantined_trashed(self, isolated_home):
        _make_skill(isolated_home, name="stale-draft", created_days_ago=45)
        counts = skill_prune.run_once()
        assert counts["trashed"] == 1
        assert counts["removed"] == 1
        # Original file gone, trash file present
        assert not (config.SKILLS_DIR / "stale-draft.md").exists()
        assert (config.SKILLS_DIR / "_trash" / "stale-draft.md").exists()

    def test_recent_quarantined_kept(self, isolated_home):
        _make_skill(isolated_home, name="new-draft", created_days_ago=5)
        counts = skill_prune.run_once()
        assert counts["trashed"] == 0
        assert (config.SKILLS_DIR / "new-draft.md").exists()

    def test_used_quarantined_kept(self, isolated_home):
        """A quarantined skill the user actually invoked is valuable."""
        _make_skill(
            isolated_home, name="used-draft", created_days_ago=45, runs=3,
        )
        counts = skill_prune.run_once()
        assert counts["trashed"] == 0
        assert (config.SKILLS_DIR / "used-draft.md").exists()


# ============================================================
# Trash → unlink
# ============================================================


class TestTrashToUnlink:
    def test_old_trash_unlinked(self, isolated_home):
        trash = config.SKILLS_DIR / "_trash"
        trash.mkdir(parents=True, exist_ok=True)
        old = trash / "ancient.md"
        old.write_text("(garbage)", encoding="utf-8")
        # Backdate mtime by 45 days.
        old_ts = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=45)
        ).timestamp()
        import os
        os.utime(old, (old_ts, old_ts))

        counts = skill_prune.run_once()
        assert counts["unlinked"] == 1
        assert not old.exists()

    def test_recent_trash_kept(self, isolated_home):
        trash = config.SKILLS_DIR / "_trash"
        trash.mkdir(parents=True, exist_ok=True)
        recent = trash / "fresh.md"
        recent.write_text("(recent)", encoding="utf-8")
        counts = skill_prune.run_once()
        assert counts["unlinked"] == 0
        assert recent.exists()


# ============================================================
# Trusted skills — NEVER auto-deleted
# ============================================================


class TestTrustedNeverDeleted:
    def test_trusted_old_skill_not_deleted(self, isolated_home):
        _make_skill(
            isolated_home, name="legacy-tool",
            state="trusted-auto",
            created_days_ago=400,
            last_promoted_days_ago=400,
            runs=20,
        )
        counts = skill_prune.run_once()
        # Marked stale, NOT deleted.
        assert counts["trashed"] == 0
        assert (config.SKILLS_DIR / "legacy-tool.md").exists()

    def test_trusted_inactive_marked_stale(self, isolated_home):
        _make_skill(
            isolated_home, name="legacy-tool",
            state="trusted-supervised",
            created_days_ago=120,
            last_promoted_days_ago=120,
            runs=5,
        )
        counts = skill_prune.run_once()
        assert counts["stale_marked"] == 1
        # Reload and confirm the frontmatter flag is there
        reloaded = skills_mod.load("legacy-tool")
        assert reloaded is not None
        fm = reloaded.raw_frontmatter
        assert "stale_warning" in fm or "stale-warning" in fm

    def test_trusted_active_not_marked(self, isolated_home):
        _make_skill(
            isolated_home, name="active-tool",
            state="trusted-auto",
            created_days_ago=10,
            last_promoted_days_ago=10,
            runs=5,
        )
        counts = skill_prune.run_once()
        assert counts["stale_marked"] == 0

    def test_stale_mark_idempotent_within_day(self, isolated_home):
        _make_skill(
            isolated_home, name="dusty",
            state="trusted-auto",
            created_days_ago=120,
            last_promoted_days_ago=120,
            runs=5,
        )
        first = skill_prune.run_once()
        assert first["stale_marked"] == 1
        second = skill_prune.run_once()
        assert second["stale_marked"] == 0


# ============================================================
# Protected via no-prune frontmatter
# ============================================================


def test_no_prune_flag_protects(isolated_home):
    _make_skill(
        isolated_home, name="precious-draft",
        state="quarantined",
        created_days_ago=400,
        runs=0,
        extra_fm={"no-prune": True},
    )
    counts = skill_prune.run_once()
    assert counts["trashed"] == 0
    assert (config.SKILLS_DIR / "precious-draft.md").exists()


# ============================================================
# Empty / no-skills edge case
# ============================================================


def test_empty_skills_dir_no_crash(isolated_home):
    counts = skill_prune.run_once()
    assert counts == {
        "removed": 0,
        "trashed": 0,
        "stale_marked": 0,
        "unlinked": 0,
    }
