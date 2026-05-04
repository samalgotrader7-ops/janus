"""Tests for v1.5 phase 6: model-callable swarm.run tool."""
from __future__ import annotations

import pytest

from janus import config, subagent
from janus.swarms import recursion, runner, spec, state
from janus.tools.swarm_run import SwarmRun


@pytest.fixture
def fake_subagent(monkeypatch):
    def _stub(spec_obj, **kw):
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output=f"ok:{spec_obj.leaf_id}",
            trace=[{"step": 0, "type": "final", "text": "ok"}],
            error=None,
        )
    monkeypatch.setattr(subagent, "_run_in_process", _stub)


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", tmp_path / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path / "runs")
    (tmp_path / "specs").mkdir()
    (tmp_path / "runs").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def reset_recursion_depth():
    recursion.reset_for_thread()
    yield
    recursion.reset_for_thread()


def _write_demo_spec(specs_dir, name="demo"):
    p = specs_dir / f"{name}.md"
    p.write_text(f"""---
name: {name}
type: swarm
description: test
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""", encoding="utf-8")


# ---------- Tool metadata ----------


def test_tool_has_correct_attributes():
    t = SwarmRun()
    assert t.name == "swarm_run"
    assert t.risk == "exec"
    assert "spec_name" in t.parameters["properties"]
    assert "inputs" in t.parameters["properties"]
    assert "spec_name" in t.parameters["required"]


def test_tool_schema_renders():
    """Schema must be valid for OpenAI tool-call format."""
    t = SwarmRun()
    s = t.schema()
    assert s["type"] == "function"
    assert s["function"]["name"] == "swarm_run"
    assert "description" in s["function"]


# ---------- run() — basic ----------


def test_run_with_missing_spec_name_errors():
    t = SwarmRun()
    out = t.run({}, lambda *a, **kw: True)
    assert "spec_name required" in out


def test_run_with_non_string_spec_name_errors():
    t = SwarmRun()
    out = t.run({"spec_name": 123}, lambda *a, **kw: True)
    assert "spec_name required" in out


def test_run_with_non_dict_inputs_errors():
    t = SwarmRun()
    out = t.run(
        {"spec_name": "x", "inputs": "not a dict"},
        lambda *a, **kw: True,
    )
    assert "inputs must be a JSON object" in out


def test_run_with_unknown_spec_errors(isolated_dirs):
    t = SwarmRun()
    out = t.run({"spec_name": "nonexistent"}, lambda *a, **kw: True)
    assert "no spec named" in out


def test_run_invokes_runner_on_known_spec(isolated_dirs, fake_subagent):
    _write_demo_spec(isolated_dirs / "specs", "demo")
    t = SwarmRun()
    out = t.run({"spec_name": "demo"}, lambda *a, **kw: True)
    assert "run_id:" in out
    assert "demo" in out
    assert "phases: 1" in out


def test_run_passes_inputs_to_runner(isolated_dirs, fake_subagent):
    p = isolated_dirs / "specs" / "withinput.md"
    p.write_text("""---
name: withinput
type: swarm
inputs:
  count:
    type: int
    required: true
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""", encoding="utf-8")
    t = SwarmRun()
    out = t.run(
        {"spec_name": "withinput", "inputs": {"count": 5}},
        lambda *a, **kw: True,
    )
    assert "run_id:" in out
    # If inputs hadn't validated, we'd see "input validation failed"
    assert "validation failed" not in out


def test_run_surfaces_input_validation_errors(isolated_dirs, fake_subagent):
    p = isolated_dirs / "specs" / "withinput.md"
    p.write_text("""---
name: withinput
type: swarm
inputs:
  count:
    type: int
    required: true
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""", encoding="utf-8")
    t = SwarmRun()
    out = t.run(
        {"spec_name": "withinput"},  # missing required input
        lambda *a, **kw: True,
    )
    assert "validation failed" in out
    assert "missing required input" in out


# ---------- Approver gate ----------


def test_run_refuses_when_approver_denies():
    t = SwarmRun()
    out = t.run(
        {"spec_name": "demo"},
        lambda *a, **kw: False,  # always deny
    )
    assert "refused" in out


def test_run_passes_capability_triple_to_approver(isolated_dirs, fake_subagent):
    _write_demo_spec(isolated_dirs / "specs", "demo")
    seen: dict = {}

    def capturing_approver(action, details, **kw):
        seen.update(kw)
        return True

    t = SwarmRun()
    t.run({"spec_name": "demo"}, capturing_approver)
    assert seen.get("capability") == ("swarm", "run", "demo")


# ---------- Recursion guard ----------


def test_run_refuses_when_recursion_depth_exceeded(isolated_dirs, fake_subagent):
    """When already at max depth, run_swarm returns recursion_depth_exceeded
    which the tool surfaces in its summary."""
    p = isolated_dirs / "specs" / "tight.md"
    p.write_text("""---
name: tight
type: swarm
budget:
  max_recursion_depth: 0
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""", encoding="utf-8")
    t = SwarmRun()
    out = t.run({"spec_name": "tight"}, lambda *a, **kw: True)
    # max_recursion_depth=0 means even a top-level spawn is blocked
    # since the depth_scope check is `depth >= max_depth` → 0 >= 0.
    assert "recursion_depth_exceeded" in out


# ---------- Output formatting ----------


def test_run_truncates_large_final_blob(isolated_dirs, monkeypatch):
    """Large final outputs get truncated with a pointer to the file."""
    _write_demo_spec(isolated_dirs / "specs", "demo")

    def _big_output_stub(spec_obj, **kw):
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="x" * 1000,  # large output
            trace=[], error=None,
        )
    monkeypatch.setattr(subagent, "_run_in_process", _big_output_stub)

    t = SwarmRun()
    out = t.run({"spec_name": "demo"}, lambda *a, **kw: True)
    assert "preview" in out
    assert "final.json" in out


def test_run_includes_final_when_small(isolated_dirs, fake_subagent):
    _write_demo_spec(isolated_dirs / "specs", "demo")
    t = SwarmRun()
    out = t.run({"spec_name": "demo"}, lambda *a, **kw: True)
    # Small output renders inline
    assert "final:" in out


# ---------- Registry inclusion ----------


def test_swarm_run_in_default_registry():
    """The model can call swarm_run via the default tool registry."""
    from janus.tools import default_registry
    reg = default_registry()
    assert "swarm_run" in reg.names()


def test_swarm_run_appears_in_schemas():
    from janus.tools import default_registry
    reg = default_registry()
    names = [s["function"]["name"] for s in reg.schemas()]
    assert "swarm_run" in names
