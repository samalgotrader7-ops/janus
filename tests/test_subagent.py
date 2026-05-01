"""Tests for Phase 8 — subagents.

Covers:
- default_registry filtering by tool_names.
- capability-only approver: grants on match, denies otherwise (never prompts).
- SubagentSpec / SubagentResult JSON round-trip.
- _run_in_process: executor invocation, log linkage via parent_id.
- run_batch: result ordering matches input regardless of completion order.
- specs_conflict: overlap detection between fs.write globs.
- Recursion guard: subagent env makes orchestrator parallel a no-op.
- Orchestrator parallel mode: invokes runner; serializes file conflicts.
- Test runner indirection: _RUNNER swap works.
"""
from __future__ import annotations
import json
import time

import pytest

from janus import (
    config,
    logger,
    orchestrator,
    planner,
    subagent,
)
from janus.subagent import (
    SubagentSpec, SubagentResult, capability_only_approver, run_batch,
    specs_conflict, write_target_globs, is_subagent_env, _run_in_process,
)
from janus.tools import default_registry
from janus.tools.capabilities import CapabilitySet


# ---------- default_registry tool_names filter ----------


def test_default_registry_full_set(janus_home):
    reg = default_registry()
    # Phase 1 + Phase 9 tools — full set must include the originals.
    names = set(reg.names())
    for legacy in {"fs_read", "fs_list", "fs_write", "shell", "web_fetch"}:
        assert legacy in names


def test_default_registry_filtered_subset(janus_home):
    reg = default_registry(tool_names=["fs_read", "web_fetch"])
    assert set(reg.names()) == {"fs_read", "web_fetch"}


def test_default_registry_empty_tool_names_yields_zero_tools(janus_home):
    reg = default_registry(tool_names=[])
    assert reg.names() == []


def test_default_registry_unknown_names_silently_dropped(janus_home):
    reg = default_registry(tool_names=["fs_read", "no_such_tool"])
    assert reg.names() == ["fs_read"]


# ---------- capability_only_approver ----------


def test_capability_only_approver_grants_on_match(janus_home):
    caps = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    approver = capability_only_approver(caps)
    assert approver("run shell", "details", capability=("shell", "exec", "git status")) is True


def test_capability_only_approver_denies_without_capability_kwarg(janus_home):
    """When the tool doesn't even pass a capability= kwarg, the inner
    deny approver fires — nothing is granted."""
    caps = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    approver = capability_only_approver(caps)
    assert approver("danger", "details") is False


def test_capability_only_approver_denies_unmatched_target(janus_home):
    caps = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    approver = capability_only_approver(caps)
    assert approver("run", "rm -rf /", capability=("shell", "exec", "rm -rf /")) is False


# ---------- SubagentSpec / Result JSON round-trip ----------


def test_subagent_spec_json_roundtrip():
    spec = SubagentSpec(
        leaf_id="a", parent_id="2026-05-01T10:00:00Z",
        description="leaf a", request="orig", label="lbl", action="goal",
        skill_body="body", memory_preamble="mem",
        tool_names=["fs_read"],
        capability_set={"fs.read": ["**"]},
    )
    blob = spec.to_json()
    restored = SubagentSpec.from_json(blob)
    assert restored.leaf_id == "a"
    assert restored.tool_names == ["fs_read"]
    assert restored.capability_set == {"fs.read": ["**"]}


def test_subagent_result_json_roundtrip():
    r = SubagentResult(
        leaf_id="x", parent_id="p",
        output="hello", trace=[{"step": 0, "type": "final", "text": "hello"}],
        error=None,
    )
    d = json.loads(r.to_json())
    assert d["leaf_id"] == "x"
    assert d["output"] == "hello"
    assert d["error"] is None


# ---------- specs_conflict ----------


def test_write_target_globs_extracts_fs_writes():
    assert write_target_globs({"fs.write": ["src/**"], "fs.read": ["**"]}) == ["src/**"]
    assert write_target_globs(None) == []
    assert write_target_globs({}) == []


def test_specs_conflict_no_writes_anywhere():
    a = {"fs.read": ["**"]}
    b = {"fs.read": ["**"]}
    assert specs_conflict(a, b) is False


def test_specs_conflict_one_side_no_writes():
    a = {"fs.write": ["src/**"]}
    b = {"fs.read": ["**"]}
    assert specs_conflict(a, b) is False


def test_specs_conflict_overlapping_writes():
    a = {"fs.write": ["src/**"]}
    b = {"fs.write": ["src/foo.py"]}
    assert specs_conflict(a, b) is True


def test_specs_conflict_disjoint_writes():
    a = {"fs.write": ["docs/**"]}
    b = {"fs.write": ["src/**"]}
    assert specs_conflict(a, b) is False


def test_specs_conflict_double_star_matches_anything():
    a = {"fs.write": ["**"]}
    b = {"fs.write": ["src/foo.py"]}
    assert specs_conflict(a, b) is True


# ---------- is_subagent_env ----------


def test_is_subagent_env_default_off():
    # Default test env should not have JANUS_IS_SUBAGENT=1.
    # (Smoke check; tests below set it explicitly when needed.)
    assert is_subagent_env() is False


def test_is_subagent_env_when_set(monkeypatch):
    monkeypatch.setenv("JANUS_IS_SUBAGENT", "1")
    assert is_subagent_env() is True


# ---------- _run_in_process ----------


def test_run_in_process_executes_and_returns_output(janus_home, fake_llm):
    """In-process subagent runs the executor and returns the final text."""
    fake_llm.append({"content": "subagent done", "tool_calls": []})
    spec = SubagentSpec(
        leaf_id="a", parent_id="parent-ts",
        description="a", request="req", label="lbl", action="goal",
        capability_set={"fs.read": ["**"]},
        tool_names=["fs_read"],
    )
    result = _run_in_process(spec)
    assert result.leaf_id == "a"
    assert result.output == "subagent done"
    assert result.error is None


def test_run_in_process_logs_with_parent_id(janus_home, fake_llm):
    """The subagent's log record must carry parent_id and type='subagent'."""
    fake_llm.append({"content": "ok", "tool_calls": []})
    spec = SubagentSpec(
        leaf_id="leaf-x", parent_id="2026-05-01T12:00:00Z",
        description="x", request="r", label="l", action="g",
        capability_set={"fs.read": ["**"]},
    )
    _run_in_process(spec)

    records = [r for r in logger.read_all() if r.get("type") == "subagent"]
    assert any(
        r["leaf_id"] == "leaf-x" and r["parent_id"] == "2026-05-01T12:00:00Z"
        for r in records
    )


# ---------- run_batch ordering ----------


def test_run_batch_returns_in_input_order(janus_home, monkeypatch):
    """Even if subagent B finishes before A, results are in input order."""
    received: list[str] = []

    def fake_runner(spec: SubagentSpec) -> SubagentResult:
        received.append(spec.leaf_id)
        # Simulate B finishing fast, A slow.
        if spec.leaf_id == "a":
            time.sleep(0.05)
        return SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output=f"out-{spec.leaf_id}", trace=[], error=None,
        )

    monkeypatch.setattr(subagent, "_RUNNER", fake_runner)
    specs = [
        SubagentSpec(leaf_id="a", parent_id="p", description="a",
                     request="r", label="l", action="g"),
        SubagentSpec(leaf_id="b", parent_id="p", description="b",
                     request="r", label="l", action="g"),
    ]
    results = run_batch(specs, concurrency=2)
    # Input order preserved regardless of completion order.
    assert [r.leaf_id for r in results] == ["a", "b"]


def test_run_batch_sequential_when_concurrency_one(janus_home, monkeypatch):
    runs = []

    def fake_runner(spec):
        runs.append(spec.leaf_id)
        return SubagentResult(leaf_id=spec.leaf_id, parent_id=spec.parent_id,
                              output="x", trace=[])
    monkeypatch.setattr(subagent, "_RUNNER", fake_runner)
    specs = [
        SubagentSpec(leaf_id=str(i), parent_id="p", description=str(i),
                     request="r", label="l", action="g")
        for i in range(3)
    ]
    results = run_batch(specs, concurrency=1)
    assert [r.leaf_id for r in results] == ["0", "1", "2"]
    assert runs == ["0", "1", "2"]


# ---------- Orchestrator parallel mode ----------


def test_orchestrator_parallel_invokes_subagent_runner(janus_home, fake_llm, monkeypatch):
    """Parallel mode routes leaves through subagent.run_subagent."""
    invoked: list[str] = []

    def fake_runner(spec: SubagentSpec) -> SubagentResult:
        invoked.append(spec.leaf_id)
        return SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output=f"sub-{spec.leaf_id}", trace=[], error=None,
        )
    monkeypatch.setattr(subagent, "_RUNNER", fake_runner)

    a = planner.PlanNode(id="a", goal="alpha")
    b = planner.PlanNode(id="b", goal="beta")  # independent of a
    root = planner.PlanNode(id="root", goal="combo", children=[a, b])

    # Only the summarizer is the LLM call — the rest go through fake_runner.
    fake_llm.append({"content": "summary: both done"})

    rr = orchestrator.run(
        original_request="do combo",
        chosen_label="combo",
        chosen_action="do combo",
        plan=root,
        base_approver=lambda *a, **kw: True,
        parallel=True,
        parent_id="parent-ts-1",
    )
    assert sorted(invoked) == ["a", "b"]
    assert {lr.id for lr in rr.leaves} == {"a", "b"}
    # parent_id propagated through the spec.
    # (The fake_runner sees it; we already checked in the SubagentResult.)


def test_orchestrator_parallel_respects_recursion_guard(
    janus_home, fake_llm, monkeypatch,
):
    """When JANUS_IS_SUBAGENT=1, parallel=True must fall back to sequential
    in-process execution (no subagent spawning)."""
    monkeypatch.setenv("JANUS_IS_SUBAGENT", "1")
    invoked = []
    monkeypatch.setattr(
        subagent, "_RUNNER",
        lambda spec: invoked.append(spec.leaf_id) or SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id, output="x", trace=[]),
    )

    a = planner.PlanNode(id="a", goal="alpha")
    b = planner.PlanNode(id="b", goal="beta", deps=["a"])
    root = planner.PlanNode(id="root", goal="combo", children=[a, b])

    # Sequential path: two executor calls + one summary.
    fake_llm.append({"content": "alpha done", "tool_calls": []})
    fake_llm.append({"content": "beta done",  "tool_calls": []})
    fake_llm.append({"content": "summary"})

    rr = orchestrator.run(
        original_request="do combo",
        chosen_label="combo",
        chosen_action="do combo",
        plan=root,
        base_approver=lambda *a, **kw: True,
        parallel=True,
        parent_id="parent-ts-2",
    )
    assert invoked == []  # no subagent spawned
    assert {lr.id for lr in rr.leaves} == {"a", "b"}


def test_orchestrator_parallel_serializes_file_conflicts(
    janus_home, fake_llm, monkeypatch,
):
    """Two leaves with overlapping fs.write globs must NOT appear in the
    same subagent batch — they go in separate waves of run_batch calls."""
    batches: list[list[str]] = []

    def fake_run_batch(specs, *, concurrency=None):
        # Each call to run_batch is one batch.
        batches.append([s.leaf_id for s in specs])
        return [
            SubagentResult(
                leaf_id=s.leaf_id, parent_id=s.parent_id,
                output=f"out-{s.leaf_id}", trace=[], error=None,
            ) for s in specs
        ]
    monkeypatch.setattr(subagent, "run_batch", fake_run_batch)

    # Two skills with overlapping fs.write globs.
    skills_dir = janus_home / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "writer-x.md").write_text("""---
name: writer-x
description: write to src
state: trusted-supervised
capabilities:
  fs.write:
    - "src/**"
created: 2026-04-30T00:00:00Z
runs: 0
---

writer x body
""", encoding="utf-8")
    (skills_dir / "writer-y.md").write_text("""---
name: writer-y
description: write to src/y
state: trusted-supervised
capabilities:
  fs.write:
    - "src/y.py"
created: 2026-04-30T00:00:00Z
runs: 0
---

writer y body
""", encoding="utf-8")

    a = planner.PlanNode(id="a", goal="write x", skill="writer-x")
    b = planner.PlanNode(id="b", goal="write y", skill="writer-y")
    root = planner.PlanNode(id="root", goal="writes", children=[a, b])

    fake_llm.append({"content": "summary"})

    orchestrator.run(
        original_request="writes",
        chosen_label="writes",
        chosen_action="writes",
        plan=root,
        base_approver=lambda *a, **kw: True,
        parallel=True,
    )
    # Two batches because of the conflict (a and b cannot share a batch).
    assert len(batches) == 2
    assert batches[0] != batches[1]
    assert {b for batch in batches for b in batch} == {"a", "b"}


def test_orchestrator_parallel_concurrency_false_stays_inprocess(
    janus_home, fake_llm, monkeypatch,
):
    """Leaves with concurrency=False must run in-process even in parallel mode."""
    spawned = []
    monkeypatch.setattr(
        subagent, "_RUNNER",
        lambda spec: spawned.append(spec.leaf_id) or SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id, output="x", trace=[]),
    )

    a = planner.PlanNode(id="a", goal="alpha", concurrency=False)
    b = planner.PlanNode(id="b", goal="beta")  # default concurrency=True
    root = planner.PlanNode(id="root", goal="combo", children=[a, b])

    # `a` runs in-process → 1 executor call. `b` runs as subagent (mocked) → 0 LLM.
    # Then summary call.
    fake_llm.append({"content": "alpha done", "tool_calls": []})
    fake_llm.append({"content": "summary"})

    orchestrator.run(
        original_request="combo", chosen_label="combo", chosen_action="combo",
        plan=root, base_approver=lambda *a, **kw: True,
        parallel=True,
    )
    # Only `b` should have been spawned as a subagent.
    assert spawned == ["b"]
