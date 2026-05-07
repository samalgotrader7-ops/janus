"""Tests for v1.30.1 — skill project_types frontmatter filter.

v1.28.4 added project-context detection (python/node/rust/go/mixed/
unknown). v1.30.1 lets skills declare which project types they apply
to via a ``project_types`` frontmatter field. ``skills.match`` filters
the candidate set by the current project's type before lexical
scoring.

Back-compat invariants pinned here:
  * Skills WITHOUT the field still match any project (existing
    skills don't need re-saving).
  * ``project_types: [any]`` is a synonym for "no field" — match
    any.
  * ``save()`` does NOT write an empty ``project_types`` list (would
    clutter every existing skill on next persist).

Filter semantics pinned here:
  * Exact string match against ProjectInfo.type. A skill with
    ``[python]`` does NOT match a "mixed" project — declare
    ``[python, mixed]`` if you want both.
  * When current project type is unknown/empty AND a skill has a
    non-empty list, the skill is excluded (conservative — the
    skill explicitly typed itself).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from janus import skills
from janus.tools.capabilities import CapabilitySet


_MARKER = "v1.30.1"


def _make_skill(name: str, *, project_types: list[str] | None = None) -> skills.Skill:
    """Test helper: construct a minimal in-memory Skill."""
    return skills.Skill(
        name=name,
        description=f"{name} test skill — does generic things",
        state="trusted-auto",
        capabilities=CapabilitySet([]),
        body="body",
        path=Path(f"/tmp/skills/{name}.md"),
        raw_frontmatter={},
        project_types=list(project_types or []),
    )


# ============================================================
# Skill.matches_project_type — pure unit tests
# ============================================================


def test_matches_no_field_matches_any():
    s = _make_skill("generic")
    assert s.matches_project_type("python") is True
    assert s.matches_project_type("node") is True
    assert s.matches_project_type("unknown") is True
    assert s.matches_project_type("") is True
    assert s.matches_project_type(None) is True


def test_matches_any_alias_matches_any():
    s = _make_skill("generic", project_types=["any"])
    assert s.matches_project_type("python") is True
    assert s.matches_project_type("rust") is True
    assert s.matches_project_type("") is True


def test_matches_python_only():
    s = _make_skill("py-only", project_types=["python"])
    assert s.matches_project_type("python") is True
    assert s.matches_project_type("node") is False
    assert s.matches_project_type("rust") is False
    assert s.matches_project_type("mixed") is False
    assert s.matches_project_type("unknown") is False


def test_matches_multiple_types():
    s = _make_skill("multi", project_types=["python", "rust"])
    assert s.matches_project_type("python") is True
    assert s.matches_project_type("rust") is True
    assert s.matches_project_type("node") is False


def test_matches_empty_current_excludes_typed_skill():
    """Skill explicitly typed; current is unknown — exclude (conservative)."""
    s = _make_skill("py-only", project_types=["python"])
    assert s.matches_project_type("") is False
    assert s.matches_project_type(None) is False


def test_matches_python_with_mixed_explicit():
    """User can opt into 'also fire in mixed projects' explicitly."""
    s = _make_skill("py-flex", project_types=["python", "mixed"])
    assert s.matches_project_type("python") is True
    assert s.matches_project_type("mixed") is True
    assert s.matches_project_type("node") is False


# ============================================================
# match() — filter behavior
# ============================================================


def test_match_excludes_wrong_type():
    py = _make_skill("py-format", project_types=["python"])
    py.description = "format python code"
    nd = _make_skill("nd-format", project_types=["node"])
    nd.description = "format python code"  # same desc → would tie lexically

    out = skills.match("format python code", [py, nd], project_type="python")
    names = [s.name for s in out]
    assert "py-format" in names
    assert "nd-format" not in names


def test_match_default_project_type_kwarg_is_none():
    """Auto-detect when project_type=None. Pass explicit "" to suppress."""
    sig = inspect.signature(skills.match)
    p = sig.parameters.get("project_type")
    assert p is not None
    assert p.default is None
    # kwarg-only — comes after *
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


def test_match_explicit_empty_skips_detection():
    """Pass project_type='' to bypass auto-detect (test & headless contexts)."""
    typed = _make_skill("py-only", project_types=["python"])
    typed.description = "search the codebase"
    untyped = _make_skill("generic")
    untyped.description = "search the codebase"

    out = skills.match("search the codebase", [typed, untyped], project_type="")
    names = [s.name for s in out]
    # Typed skill excluded (current=""); untyped kept.
    assert "py-only" not in names
    assert "generic" in names


def test_match_preserves_lexical_scoring_after_filter():
    """Filter happens BEFORE scoring; ranking still works on the survivors."""
    a = _make_skill("py-better", project_types=["python"])
    a.description = "format python code well"
    b = _make_skill("py-other", project_types=["python"])
    b.description = "format something unrelated"
    out = skills.match("format python code", [a, b], project_type="python")
    # Both pass the type filter; lexical ordering puts a > b.
    assert out[0].name == "py-better"


def test_match_empty_skills_returns_empty_list():
    out = skills.match("anything", [], project_type="python")
    assert out == []


def test_match_typed_skill_in_unknown_project():
    """Behavioral pin: skill `[python]` excluded when project=='unknown'."""
    s = _make_skill("py-only", project_types=["python"])
    s.description = "test command"
    out = skills.match("test command", [s], project_type="unknown")
    assert out == []


def test_match_back_compat_no_project_types_field():
    """Skills without the field never get filtered out by the new code."""
    s = _make_skill("legacy")
    s.description = "legacy thing"
    for proj in ("python", "node", "rust", "go", "mixed", "unknown", ""):
        out = skills.match("legacy thing", [s], project_type=proj)
        assert out == [s], f"failed for project_type={proj!r}"


# ============================================================
# load_path — frontmatter parsing
# ============================================================


def test_load_path_parses_yaml_list(tmp_path):
    skill_md = tmp_path / "py-fmt.md"
    skill_md.write_text(
        "---\n"
        "name: py-fmt\n"
        "description: format python\n"
        "state: trusted-auto\n"
        "project_types:\n"
        "  - python\n"
        "  - mixed\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    s = skills.load_path(skill_md)
    assert s.project_types == ["python", "mixed"]


def test_load_path_parses_scalar_string(tmp_path):
    """Tolerate ``project_types: python`` (single value as scalar)."""
    skill_md = tmp_path / "py.md"
    skill_md.write_text(
        "---\n"
        "name: py\n"
        "description: x\n"
        "state: trusted-auto\n"
        "project_types: python\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    s = skills.load_path(skill_md)
    assert s.project_types == ["python"]


def test_load_path_parses_comma_separated(tmp_path):
    """Tolerate ``project_types: python, node`` (comma-separated scalar)."""
    skill_md = tmp_path / "x.md"
    skill_md.write_text(
        "---\n"
        "name: x\n"
        "description: x\n"
        "state: trusted-auto\n"
        "project_types: python, rust\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    s = skills.load_path(skill_md)
    assert s.project_types == ["python", "rust"]


def test_load_path_no_field_yields_empty_list(tmp_path):
    skill_md = tmp_path / "legacy.md"
    skill_md.write_text(
        "---\n"
        "name: legacy\n"
        "description: x\n"
        "state: trusted-auto\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    s = skills.load_path(skill_md)
    assert s.project_types == []


def test_load_path_dash_alias(tmp_path):
    """``project-types`` (dashed) accepted as alias for ``project_types``."""
    skill_md = tmp_path / "alt.md"
    skill_md.write_text(
        "---\n"
        "name: alt\n"
        "description: x\n"
        "state: trusted-auto\n"
        "project-types:\n"
        "  - go\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    s = skills.load_path(skill_md)
    assert s.project_types == ["go"]


def test_load_path_normalizes_case(tmp_path):
    """Values are lower-cased on load — Python/PYTHON/python all work."""
    skill_md = tmp_path / "case.md"
    skill_md.write_text(
        "---\n"
        "name: case\n"
        "description: x\n"
        "state: trusted-auto\n"
        "project_types:\n"
        "  - Python\n"
        "  - NODE\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    s = skills.load_path(skill_md)
    assert s.project_types == ["python", "node"]


# ============================================================
# save() — round-trip + clean omission
# ============================================================


def test_save_persists_project_types(tmp_path, monkeypatch):
    monkeypatch.setattr(skills.config, "SKILLS_DIR", tmp_path)
    p = tmp_path / "py.md"
    s = skills.Skill(
        name="py",
        description="x",
        state="trusted-auto",
        capabilities=CapabilitySet([]),
        body="body",
        path=p,
        raw_frontmatter={},
        project_types=["python", "mixed"],
    )
    skills.save(s)
    text = p.read_text(encoding="utf-8")
    # Field present in serialized frontmatter
    assert "project_types" in text
    assert "python" in text
    # Round-trip
    reloaded = skills.load_path(p)
    assert reloaded.project_types == ["python", "mixed"]


def test_save_omits_empty_project_types(tmp_path, monkeypatch):
    """An empty list (back-compat default) must NOT appear in the file —
    don't pollute legacy skills on next save."""
    monkeypatch.setattr(skills.config, "SKILLS_DIR", tmp_path)
    p = tmp_path / "legacy.md"
    s = skills.Skill(
        name="legacy",
        description="x",
        state="trusted-auto",
        capabilities=CapabilitySet([]),
        body="body",
        path=p,
        raw_frontmatter={},
        project_types=[],
    )
    skills.save(s)
    text = p.read_text(encoding="utf-8")
    assert "project_types" not in text
    assert "project-types" not in text


def test_save_strips_stale_field_when_cleared(tmp_path, monkeypatch):
    """If a skill once had project_types but is saved with [] now,
    the field must be removed from the file (not preserved via
    raw_frontmatter)."""
    monkeypatch.setattr(skills.config, "SKILLS_DIR", tmp_path)
    p = tmp_path / "x.md"
    s = skills.Skill(
        name="x",
        description="x",
        state="trusted-auto",
        capabilities=CapabilitySet([]),
        body="body",
        path=p,
        raw_frontmatter={"project_types": ["python"]},
        project_types=[],  # cleared
    )
    skills.save(s)
    text = p.read_text(encoding="utf-8")
    assert "project_types" not in text


# ============================================================
# Module surface pins
# ============================================================


def test_known_project_types_constant_exists():
    assert hasattr(skills, "KNOWN_PROJECT_TYPES")
    known = skills.KNOWN_PROJECT_TYPES
    assert "python" in known
    assert "node" in known
    assert "rust" in known
    assert "go" in known
    assert "mixed" in known
    assert "unknown" in known
    assert "any" in known


def test_skill_dataclass_has_project_types_field():
    """Source-pin: dataclass field with default empty list — defaults
    must remain mutable-safe (default_factory)."""
    src = inspect.getsource(skills.Skill)
    assert "project_types" in src
    assert _MARKER in src


def test_match_source_pins_project_type_filter():
    src = inspect.getsource(skills.match)
    assert _MARKER in src
    assert "matches_project_type" in src
    assert "project_detect" in src
