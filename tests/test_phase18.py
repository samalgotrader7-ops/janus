"""Tests for Phase 18 — trust score + skills-market diff view."""
from __future__ import annotations

import pytest

from janus import config, skills, skills_market


# ---------- trust_score / trust_label ----------


def _mk(janus_home, name, runs=0, success=0, fail=0):
    text = f"""---
name: {name}
description: a skill
state: quarantined
capabilities:
  shell.exec:
    - "git *"
created: 2026-04-30T00:00:00Z
runs: {runs}
success: {success}
fail: {fail}
---

body for {name}
"""
    p = config.SKILLS_DIR / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return skills.load(name)


def test_trust_score_none_when_no_runs(janus_home):
    s = _mk(janus_home, "x")
    assert s.trust_score() is None
    assert s.trust_label() == "—"


def test_trust_score_perfect_runs(janus_home):
    s = _mk(janus_home, "x", runs=10, success=10, fail=0)
    assert s.trust_score() == 1.0
    assert s.trust_label() == "★★★"


def test_trust_score_75_percent(janus_home):
    s = _mk(janus_home, "x", runs=4, success=3, fail=1)
    assert s.trust_score() == 0.75
    assert s.trust_label() == "★★·"


def test_trust_score_low(janus_home):
    s = _mk(janus_home, "x", runs=4, success=1, fail=3)
    assert s.trust_score() == 0.25
    assert s.trust_label() == "···"


def test_trust_score_clamped_when_success_exceeds_runs(janus_home):
    """Defensive: bad data shouldn't yield > 1.0."""
    s = _mk(janus_home, "x", runs=3, success=5, fail=0)
    assert s.trust_score() == 1.0


# ---------- diff_against_neighbor ----------


def test_diff_against_neighbor_returns_none_when_no_neighbor(janus_home, tmp_path):
    src = tmp_path / "fresh.md"
    src.write_text("""---
name: brand-new
description: x
state: quarantined
---

body
""", encoding="utf-8")
    p = skills_market.import_skill(str(src))
    # No other skills present; neighbor should be None.
    assert skills_market.diff_against_neighbor(p) is None


def test_diff_against_neighbor_finds_renamed_collision(janus_home, tmp_path):
    # Pre-install a skill named "writer".
    _mk(janus_home, "writer")
    # Import a skill with the same name → gets installed as writer-2.md
    src = tmp_path / "writer.md"
    src.write_text("""---
name: writer
description: revised
state: trusted-auto
---

UPDATED body for writer
""", encoding="utf-8")
    p = skills_market.import_skill(str(src))
    assert p.stem.startswith("writer")
    diff = skills_market.diff_against_neighbor(p)
    assert diff is not None
    assert "writer" in diff
    # The body changed substantially.
    assert "UPDATED" in diff or "+UPDATED" in diff


def test_diff_against_neighbor_no_match_for_unrelated(janus_home, tmp_path):
    _mk(janus_home, "writer")
    src = tmp_path / "totally-different.md"
    src.write_text("""---
name: zzzz-unrelated
description: x
state: quarantined
---

unrelated body
""", encoding="utf-8")
    p = skills_market.import_skill(str(src))
    # Neighbor list excludes self; nothing else looks like "zzzz-unrelated".
    assert skills_market.diff_against_neighbor(p) is None
