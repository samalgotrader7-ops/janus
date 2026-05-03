"""Tests for v1.4 budget enforcement: SwarmBudget kill switch, cost
attribution via thread-local, per-swarm ledger."""
from __future__ import annotations
import json
import time
from pathlib import Path

import pytest

from janus import config, cost, subagent
from janus.swarms import budget as budget_mod
from janus.swarms import runner, spec, state


# ---------- BudgetVerdict ----------


def test_verdict_ok():
    v = budget_mod.BudgetVerdict.ok()
    assert v.allowed is True
    assert v.reason == ""


def test_verdict_deny():
    v = budget_mod.BudgetVerdict.deny("nope")
    assert v.allowed is False
    assert "nope" in v.reason


# ---------- SwarmBudget primitives ----------


def _budget(**overrides) -> spec.Budget:
    base = dict(
        max_usd=1.00, max_wallclock_s=60, max_subagents=10,
        max_recursion_depth=2, max_total_tool_calls=100,
        max_completion_tokens_per_role=800,
    )
    base.update(overrides)
    return spec.Budget(**base)


def test_budget_initial_state_passes_check(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget())
    assert b.check("nonexistent-run").allowed is True


def test_budget_register_dispatch_increments(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget(max_subagents=3))
    b.register_dispatch()
    b.register_dispatch()
    snap = b.snapshot("r")
    assert snap["n_subagents"] == 2


def test_budget_register_complete_counts_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget())
    b.register_complete(5)
    b.register_complete(3)
    snap = b.snapshot("r")
    assert snap["n_tool_calls"] == 8


def test_budget_kills_on_subagent_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget(max_subagents=2))
    b.register_dispatch()
    b.register_dispatch()
    b.register_dispatch()  # over the cap
    v = b.check("r")
    assert not v.allowed
    assert "max_subagents_exceeded" in v.reason


def test_budget_kills_on_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget(max_total_tool_calls=10))
    b.register_complete(20)
    v = b.check("r")
    assert not v.allowed
    assert "max_total_tool_calls_exceeded" in v.reason


def test_budget_can_dispatch_n_more(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget(max_subagents=5))
    b.register_dispatch()
    b.register_dispatch()
    assert b.can_dispatch_n_more(3).allowed
    assert not b.can_dispatch_n_more(4).allowed


def test_budget_kills_on_wallclock(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget(max_wallclock_s=1))
    # Force the start time backward so wallclock check trips.
    b.state.started_at = time.time() - 10
    v = b.check("r")
    assert not v.allowed
    assert "wallclock_exceeded" in v.reason


def test_budget_kills_on_usd(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "swarm-test-budget"
    (tmp_path / rid).mkdir(parents=True, exist_ok=True)

    b = budget_mod.SwarmBudget(_budget(max_usd=0.001))

    # Write a cost row showing $0.01 spent (above the $0.001 cap).
    cost.record_per_subagent(
        swarm_run_id=rid, agent_id="x-001-aaaa", role="x", phase="p",
        model="test", prompt_tokens=100, completion_tokens=50, usd=0.01,
    )

    v = b.check(rid)
    assert not v.allowed
    assert "max_usd_exceeded" in v.reason


# ---------- Cost attribution via thread-local ----------


def test_set_and_clear_active_subagent_idempotent():
    cost.clear_active_subagent()  # baseline
    cost.set_active_subagent(
        swarm_run_id="r1", agent_id="a-001-aaaa", role="r", phase="p",
    )
    assert cost._THREAD_LOCAL.swarm_run_id == "r1"
    cost.clear_active_subagent()
    assert not hasattr(cost._THREAD_LOCAL, "swarm_run_id")
    # Idempotent — no AttributeError on repeated clear.
    cost.clear_active_subagent()


def test_record_writes_per_swarm_when_attributed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "swarm-attrib"
    (tmp_path / rid).mkdir(parents=True, exist_ok=True)

    cost.set_active_subagent(
        swarm_run_id=rid, agent_id="scraper-000-aaaa",
        role="scraper", phase="collect",
    )
    try:
        cost.record(
            "anthropic/claude-haiku-4-5",
            {"prompt_tokens": 100, "completion_tokens": 50},
        )
    finally:
        cost.clear_active_subagent()

    # Per-swarm ledger should have one row for this sub-agent.
    summary = cost.per_swarm_summary(rid)
    assert summary.calls == 1
    assert summary.prompt_tokens == 100
    assert summary.completion_tokens == 50
    assert summary.usd > 0


def test_record_skips_per_swarm_when_unattributed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    cost.clear_active_subagent()
    cost.record(
        "openai/gpt-4o-mini",
        {"prompt_tokens": 100, "completion_tokens": 50},
    )
    # No swarm dir, no ledger created.
    assert not (tmp_path / "swarm-x").exists()


def test_per_swarm_summary_filters_by_role(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "swarm-multi-role"
    (tmp_path / rid).mkdir(parents=True, exist_ok=True)

    for role in ["scraper", "scraper", "reporter"]:
        cost.record_per_subagent(
            swarm_run_id=rid, agent_id=f"{role}-000-aaaa",
            role=role, phase="p",
            model="m", prompt_tokens=10, completion_tokens=5, usd=0.001,
        )

    scrapers = cost.per_swarm_summary(rid, role="scraper")
    reporters = cost.per_swarm_summary(rid, role="reporter")
    assert scrapers.calls == 2
    assert reporters.calls == 1


def test_per_swarm_summary_filters_by_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "swarm-phases"
    (tmp_path / rid).mkdir(parents=True, exist_ok=True)
    cost.record_per_subagent(
        swarm_run_id=rid, agent_id="a", role="r", phase="p1",
        model="m", prompt_tokens=10, completion_tokens=5, usd=0.001,
    )
    cost.record_per_subagent(
        swarm_run_id=rid, agent_id="b", role="r", phase="p2",
        model="m", prompt_tokens=20, completion_tokens=10, usd=0.002,
    )
    p1 = cost.per_swarm_summary(rid, phase="p1")
    p2 = cost.per_swarm_summary(rid, phase="p2")
    assert p1.calls == 1 and p1.prompt_tokens == 10
    assert p2.calls == 1 and p2.prompt_tokens == 20


def test_render_per_swarm_includes_role_and_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "swarm-render"
    (tmp_path / rid).mkdir(parents=True, exist_ok=True)
    cost.record_per_subagent(
        swarm_run_id=rid, agent_id="x", role="scraper", phase="collect",
        model="m", prompt_tokens=100, completion_tokens=50, usd=0.01,
    )
    out = cost.render_per_swarm(rid)
    assert "swarm-render" in out
    assert "scraper" in out
    assert "collect" in out


# ---------- Runner kill switch end-to-end ----------


@pytest.fixture
def fake_subagent(monkeypatch):
    state_box: dict = {"calls": [], "responder": None}

    def _stub(spec_obj):
        state_box["calls"].append(spec_obj)
        if state_box["responder"]:
            return state_box["responder"](spec_obj)
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output=f"ok:{spec_obj.leaf_id}",
            trace=[{"step": 0, "type": "final", "text": "ok"}],
            error=None,
        )

    monkeypatch.setattr(subagent, "_run_in_process", _stub)
    return state_box


def test_runner_kills_when_max_subagents_dispatched_pre_phase(
    tmp_path, monkeypatch, fake_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: tight
type: swarm
budget:
  max_subagents: 1
phases:
  p1:
    pattern: single
    role: r
    aggregator: concat
  p2:
    pattern: single
    role: r
    aggregator: concat
    depends_on: p1
---
body
""")
    result = runner.run_swarm(s, inputs={})
    # First phase runs (1 sub-agent dispatched). Second phase pre-check
    # finds the cap already at 1 → phase_truncated to 0 sub-agents (or
    # killed by check between phases). In our impl we check BEFORE the
    # phase runs, so phase 2 either runs with 0 tasks or budget-fails.
    # Since check() fires the budget kill, expect swarm error.
    assert result.error is not None
    assert "max_subagents" in result.error


def test_runner_kills_when_usd_exceeds_at_phase_boundary(
    tmp_path, monkeypatch, fake_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: cheap
type: swarm
budget:
  max_usd: 0.001
phases:
  p1:
    pattern: single
    role: r
    aggregator: concat
  p2:
    pattern: single
    role: r
    aggregator: concat
    depends_on: p1
---
body
""")
    # Stub: each sub-agent records cost well above $0.001.
    # claude-opus-4-7 is $15/M in + $75/M out → 100k input + 100k output ≈ $9.
    def _stub_with_cost(spec_obj):
        cost.record(
            "anthropic/claude-opus-4-7",
            {"prompt_tokens": 100000, "completion_tokens": 100000},
        )
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="ok", trace=[], error=None,
        )
    fake_subagent["responder"] = _stub_with_cost

    result = runner.run_swarm(s, inputs={})
    assert result.error is not None
    assert "max_usd" in result.error
    # Second phase never ran
    assert len(result.phases) == 1


def test_runner_records_budget_snapshot_in_timeline(
    tmp_path, monkeypatch, fake_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: snap
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    result = runner.run_swarm(s, inputs={})
    timeline = state.read_timeline(result.run_id)
    # The phase_done event has a budget_snapshot field
    phase_done = [e for e in timeline if e["type"] == "phase_done"]
    assert phase_done
    assert "budget_snapshot" in phase_done[0]
    snap = phase_done[0]["budget_snapshot"]
    assert "usd_spent" in snap
    assert "n_subagents" in snap
    assert snap["n_subagents"] == 1


def test_runner_counts_tool_calls_per_subagent(
    tmp_path, monkeypatch, fake_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: tools
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    fake_subagent["responder"] = lambda sp: subagent.SubagentResult(
        leaf_id=sp.leaf_id, parent_id=sp.parent_id,
        output="ok",
        trace=[
            {"step": 0, "type": "tool_call", "tool": "fs_read"},
            {"step": 1, "type": "tool_call", "tool": "web_fetch"},
            {"step": 2, "type": "final", "text": "ok"},
        ],
        error=None,
    )
    result = runner.run_swarm(s, inputs={})
    timeline = state.read_timeline(result.run_id)
    sub_done = [e for e in timeline if e["type"] == "subagent_done"]
    assert sub_done[0]["tool_calls"] == 2


def test_runner_attributes_cost_to_per_swarm_ledger(
    tmp_path, monkeypatch, fake_subagent,
):
    """End-to-end: sub-agents that record cost via the standard
    cost.record() path land in the swarm-local cost.jsonl."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    def _stub(spec_obj):
        cost.record(
            "anthropic/claude-haiku-4-5",
            {"prompt_tokens": 100, "completion_tokens": 50},
        )
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="ok", trace=[], error=None,
        )
    fake_subagent["responder"] = _stub

    s = spec.parse_spec("""---
name: attrib
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    result = runner.run_swarm(s, inputs={})
    summary = cost.per_swarm_summary(result.run_id)
    assert summary.calls == 1
    assert summary.prompt_tokens == 100
    assert summary.usd > 0


# ---------- Regression: budget changes don't break empty/zero cases ----------


def test_zero_max_total_tool_calls_nonzero_budget_passes_initially(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    b = budget_mod.SwarmBudget(_budget(max_total_tool_calls=10))
    # No registrations yet — should pass.
    assert b.check("nonexistent").allowed
