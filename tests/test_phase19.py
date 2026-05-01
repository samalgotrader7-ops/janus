"""Tests for Phase 19 — public capability lib + multi-skill compose."""
from __future__ import annotations

import pytest

from janus import config, security, planner, orchestrator, skills as skills_mod


# ---------- public security re-exports ----------


def test_security_exports_capability_classes():
    assert hasattr(security, "Capability")
    assert hasattr(security, "CapabilitySet")
    assert hasattr(security, "resolve_within")


def test_capabilityset_works_through_public_api():
    caps = security.CapabilitySet.from_dict({
        "shell.exec": ["git *"],
    })
    assert caps.grants("shell", "exec", "git status") is True
    assert caps.grants("shell", "exec", "rm -rf /") is False


def test_resolve_within_accepts_path_inside(janus_home):
    p = security.resolve_within(config.WORKSPACE, "x.txt")
    assert str(p).startswith(str(config.WORKSPACE))


def test_resolve_within_refuses_dot_dot(janus_home):
    with pytest.raises(ValueError):
        security.resolve_within(config.WORKSPACE, "../../etc/passwd")


def test_resolve_within_refuses_absolute_outside(janus_home):
    with pytest.raises(ValueError):
        security.resolve_within(config.WORKSPACE, "/etc/passwd")


def test_resolve_within_handles_string_workspace(janus_home):
    """`workspace` arg accepts str, not just Path — easier interop."""
    p = security.resolve_within(str(config.WORKSPACE), "sub/x.txt")
    assert str(p).startswith(str(config.WORKSPACE))


# ---------- multi-skill compose ----------


def _mk_skill(janus_home, name, body, caps_dict):
    parts = ["---", f"name: {name}", "description: x", "state: trusted-supervised"]
    if caps_dict:
        parts.append("capabilities:")
        for k, v in caps_dict.items():
            parts.append(f"  {k}:")
            for g in v:
                parts.append(f'    - "{g}"')
    parts.append("---")
    parts.append("")
    parts.append(body)
    text = "\n".join(parts)
    p = config.SKILLS_DIR / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return skills_mod.load(name)


def test_resolve_leaf_skill_singular(janus_home):
    _mk_skill(janus_home, "alpha", "alpha body", {"fs.read": ["**"]})
    leaf = planner.PlanNode(id="x", goal="g", skill="alpha")
    s = orchestrator._resolve_leaf_skill(leaf, attached=None)
    assert s.name == "alpha"
    assert "alpha body" in s.body


def test_resolve_leaf_skill_falls_back_to_attached(janus_home):
    attached = _mk_skill(janus_home, "fallback", "x", {"fs.read": ["**"]})
    leaf = planner.PlanNode(id="x", goal="g")
    s = orchestrator._resolve_leaf_skill(leaf, attached=attached)
    assert s is attached


def test_resolve_leaf_skill_compose_unions_caps(janus_home):
    _mk_skill(janus_home, "reader", "READER", {"fs.read": ["**"]})
    _mk_skill(janus_home, "writer", "WRITER",
              {"fs.write": ["src/**"], "shell.exec": ["git *"]})
    leaf = planner.PlanNode(id="x", goal="g", skills=["reader", "writer"])
    s = orchestrator._resolve_leaf_skill(leaf, attached=None)
    # Capability union.
    assert s.capabilities.grants("fs", "read", "any/file")
    assert s.capabilities.grants("fs", "write", "src/main.py")
    assert s.capabilities.grants("shell", "exec", "git status")
    # Body concatenation in list order.
    assert "READER" in s.body and "WRITER" in s.body
    assert s.body.index("READER") < s.body.index("WRITER")
    # Composed skill is synthetic — not persisted.
    assert s.runs == 0
    assert "reader" in s.name and "writer" in s.name


def test_resolve_leaf_skill_compose_dedupes_caps(janus_home):
    _mk_skill(janus_home, "a", "A", {"fs.read": ["**"]})
    _mk_skill(janus_home, "b", "B", {"fs.read": ["**"]})
    leaf = planner.PlanNode(id="x", goal="g", skills=["a", "b"])
    s = orchestrator._resolve_leaf_skill(leaf, attached=None)
    # Same (tool, verb, globs) tuple should appear once.
    fs_read_caps = [c for c in s.capabilities.caps
                    if c.tool == "fs" and c.verb == "read"]
    assert len(fs_read_caps) == 1


def test_resolve_leaf_skill_compose_skips_missing_skills(janus_home):
    _mk_skill(janus_home, "real", "REAL", {"fs.read": ["**"]})
    leaf = planner.PlanNode(id="x", goal="g", skills=["real", "does-not-exist"])
    s = orchestrator._resolve_leaf_skill(leaf, attached=None)
    assert "REAL" in s.body
    # 'does-not-exist' silently dropped — composed skill is just "real".
    assert s.name == "real"


def test_planner_coerces_skills_field(janus_home):
    """_coerce_tree picks up the new `skills` plural field from JSON."""
    raw = {
        "id": "leaf-a",
        "goal": "do x",
        "skills": ["one", "two"],
    }
    node = planner._coerce_tree(raw, depth=0)
    assert node.skills == ["one", "two"]


def test_planner_skills_takes_precedence_over_skill(janus_home):
    """If both `skill` and `skills` are present in a plan node, both are
    accepted; the orchestrator's resolver prefers `skills`."""
    raw = {"id": "x", "goal": "g", "skill": "one", "skills": ["two", "three"]}
    node = planner._coerce_tree(raw, depth=0)
    assert node.skill == "one"
    assert node.skills == ["two", "three"]
