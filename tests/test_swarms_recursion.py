"""Tests for v1.4 swarm recursion guard via threading.local depth.

The model-callable swarm.run tool lands in v1.5 — that's when the depth
guard actually fires. v1.4 ships the plumbing so depth tracks correctly
and the runner refuses spawns that would exceed spec.budget.max_recursion_depth.
"""
from __future__ import annotations
import threading

import pytest

from janus import config, subagent
from janus.swarms import recursion, runner, spec


@pytest.fixture(autouse=True)
def reset_depth():
    """Tests share threading state — clear depth before/after each."""
    recursion.reset_for_thread()
    yield
    recursion.reset_for_thread()


# ---------- Primitive ----------


def test_depth_starts_at_zero():
    assert recursion.swarm_depth() == 0


def test_depth_scope_increments_and_decrements():
    assert recursion.swarm_depth() == 0
    with recursion.depth_scope() as d:
        assert d == 1
        assert recursion.swarm_depth() == 1
    assert recursion.swarm_depth() == 0


def test_depth_scope_nests():
    with recursion.depth_scope():
        assert recursion.swarm_depth() == 1
        with recursion.depth_scope():
            assert recursion.swarm_depth() == 2
            with recursion.depth_scope():
                assert recursion.swarm_depth() == 3
            assert recursion.swarm_depth() == 2
        assert recursion.swarm_depth() == 1
    assert recursion.swarm_depth() == 0


def test_depth_scope_decrements_on_exception():
    """Critical: even if the with-body raises, depth must restore."""
    try:
        with recursion.depth_scope():
            assert recursion.swarm_depth() == 1
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert recursion.swarm_depth() == 0


def test_exceeds_recursion_depth_at_zero():
    """At depth 0, max_depth=0 means no nested allowed."""
    assert recursion.exceeds_recursion_depth(0) is True
    assert recursion.exceeds_recursion_depth(1) is False
    assert recursion.exceeds_recursion_depth(5) is False


def test_exceeds_recursion_depth_inside_scope():
    """At depth 1 (inside one swarm), max_depth=1 means no further nest."""
    with recursion.depth_scope():
        assert recursion.exceeds_recursion_depth(1) is True
        assert recursion.exceeds_recursion_depth(2) is False


def test_depth_isolated_per_thread():
    """Sub-agent threads have their OWN depth, not the parent's."""
    parent_depths: list[int] = []
    child_depths: list[int] = []

    def child():
        # Fresh thread → depth starts at 0 even if parent is in a swarm
        child_depths.append(recursion.swarm_depth())
        with recursion.depth_scope():
            child_depths.append(recursion.swarm_depth())

    with recursion.depth_scope():
        parent_depths.append(recursion.swarm_depth())
        t = threading.Thread(target=child)
        t.start()
        t.join()
        parent_depths.append(recursion.swarm_depth())

    assert parent_depths == [1, 1]
    assert child_depths == [0, 1]


def test_reset_for_thread():
    with recursion.depth_scope():
        assert recursion.swarm_depth() == 1
        recursion.reset_for_thread()
        assert recursion.swarm_depth() == 0


# ---------- Runner integration ----------


@pytest.fixture
def fake_subagent(monkeypatch):
    state_box: dict = {"calls": 0}

    def _stub(spec_obj, **kw):
        state_box["calls"] += 1
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="ok", trace=[], error=None,
        )

    monkeypatch.setattr(subagent, "_run_in_process", _stub)
    return state_box


def test_run_swarm_increments_depth_during_run(
    tmp_path, monkeypatch, fake_subagent,
):
    """Inside a sub-agent's stub, swarm_depth() should be > 0."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    seen_depths: list[int] = []

    def _stub_with_depth(spec_obj, **kw):
        seen_depths.append(recursion.swarm_depth())
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="ok", trace=[], error=None,
        )
    monkeypatch.setattr(subagent, "_run_in_process", _stub_with_depth)

    s = spec.parse_spec("""---
name: depth-track
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
    assert result.error is None
    # The sub-agent ran in its OWN thread (ThreadPoolExecutor) — but with
    # only 1 sub-agent the runner uses the inline path (no ThreadPool).
    # Either way, the sub-agent runs in some thread; what we check is
    # that the COORDINATOR's depth was 1 during the run.
    # Since the inline path runs in the coordinator thread, depth = 1 was
    # visible from inside the stub.
    assert seen_depths == [1]


def test_run_swarm_restores_depth_after_completion(
    tmp_path, monkeypatch, fake_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: restore
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    assert recursion.swarm_depth() == 0
    runner.run_swarm(s, inputs={})
    assert recursion.swarm_depth() == 0


def test_run_swarm_refuses_when_recursion_would_exceed(
    tmp_path, monkeypatch, fake_subagent,
):
    """Pre-bumping depth past the spec's max_recursion_depth makes the
    runner refuse with recursion_depth_exceeded."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: maxed
type: swarm
budget:
  max_recursion_depth: 1
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    # Pre-bump depth as if we were already inside a swarm.
    with recursion.depth_scope():  # depth=1 — equal to max
        result = runner.run_swarm(s, inputs={})
    assert result.error is not None
    assert "recursion_depth_exceeded" in result.error
    # No sub-agent dispatched.
    assert fake_subagent["calls"] == 0


def test_run_swarm_allows_nested_within_max(
    tmp_path, monkeypatch, fake_subagent,
):
    """If depth is below max, nested swarm runs."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: nested
type: swarm
budget:
  max_recursion_depth: 2
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    # Pre-bump to depth=1. max_recursion_depth=2 → still allowed.
    with recursion.depth_scope():
        result = runner.run_swarm(s, inputs={})
    assert result.error is None
    assert fake_subagent["calls"] == 1


def test_run_swarm_restores_depth_on_exception(
    tmp_path, monkeypatch, fake_subagent,
):
    """If something inside the swarm raises (uncaught), depth must
    still restore — the depth_scope context manager handles this."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    def _stub_that_blows_up_indirectly(spec_obj, **kw):
        # The runner catches per-sub-agent exceptions, so the swarm
        # itself doesn't raise. But verify depth is restored regardless.
        raise RuntimeError("boom")

    monkeypatch.setattr(subagent, "_run_in_process",
                        _stub_that_blows_up_indirectly)
    s = spec.parse_spec("""---
name: safe-restore
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    runner.run_swarm(s, inputs={})
    assert recursion.swarm_depth() == 0
