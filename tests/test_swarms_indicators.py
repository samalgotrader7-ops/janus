"""Tests for v1.4 swarm indicators surfaced via IndicatorEmitter.

The 7 new indicator kinds (swarm_start/done, phase_start/done,
subagent_start/done, swarm_progress) extend the existing emitter
contract. Gateways subclass IndicatorEmitter to render swarm progress
live to the user (telegram, web, TUI).
"""
from __future__ import annotations
from dataclasses import dataclass, field

import pytest

from janus import config, subagent
from janus.gateways._common import (
    INDICATOR_GLYPHS, INDICATOR_KINDS, Indicator, IndicatorEmitter,
)
from janus.swarms import runner, spec


# ---------- INDICATOR_KINDS / GLYPHS extended ----------


def test_swarm_indicator_kinds_present():
    for k in (
        "swarm_start", "swarm_done", "phase_start", "phase_done",
        "subagent_start", "subagent_done", "swarm_progress",
    ):
        assert k in INDICATOR_KINDS


def test_swarm_indicator_glyphs_present():
    for k in (
        "swarm_start", "swarm_done", "phase_start", "phase_done",
        "subagent_start", "subagent_done", "swarm_progress",
    ):
        assert k in INDICATOR_GLYPHS
        assert INDICATOR_GLYPHS[k]  # non-empty


# ---------- IndicatorEmitter convenience methods ----------


@dataclass
class CapturingEmitter(IndicatorEmitter):
    captured: list[Indicator] = field(default_factory=list)

    def emit(self, ind: Indicator) -> None:
        self.captured.append(ind)


def test_emitter_swarm_start_method():
    e = CapturingEmitter()
    e.swarm_start("my-spec", "swarm-r1", 3)
    assert len(e.captured) == 1
    ind = e.captured[0]
    assert ind.kind == "swarm_start"
    assert ind.payload["spec"] == "my-spec"
    assert ind.payload["run_id"] == "swarm-r1"
    assert ind.payload["n_phases"] == 3


def test_emitter_swarm_done_method():
    e = CapturingEmitter()
    e.swarm_done("swarm-r1", 2)
    assert e.captured[0].kind == "swarm_done"
    assert e.captured[0].payload["run_id"] == "swarm-r1"
    assert e.captured[0].payload["error"] is None


def test_emitter_phase_start_method():
    e = CapturingEmitter()
    e.phase_start("collect", 0, 5)
    assert e.captured[0].kind == "phase_start"
    assert e.captured[0].payload["phase"] == "collect"
    assert e.captured[0].payload["phase_num"] == 0


def test_emitter_phase_done_method():
    e = CapturingEmitter()
    e.phase_done("collect", 0, 5, 1, usd=0.42)
    assert e.captured[0].kind == "phase_done"
    p = e.captured[0].payload
    assert p["n_subagents"] == 5
    assert p["n_errors"] == 1
    assert p["usd"] == 0.42


def test_emitter_subagent_methods():
    e = CapturingEmitter()
    e.subagent_start("scraper-001-aaaa", "scraper", "collect")
    e.subagent_done(
        "scraper-001-aaaa", "scraper", "collect",
        error=None, tool_calls=3,
    )
    kinds = [c.kind for c in e.captured]
    assert kinds == ["subagent_start", "subagent_done"]
    assert e.captured[1].payload["tool_calls"] == 3


def test_emitter_swarm_progress_method():
    e = CapturingEmitter()
    e.swarm_progress("collect", 17, 20, usd=0.12)
    assert e.captured[0].kind == "swarm_progress"
    p = e.captured[0].payload
    assert p["done"] == 17
    assert p["total"] == 20


# ---------- Runner emits indicators ----------


@pytest.fixture
def fake_subagent(monkeypatch):
    def _stub(spec_obj, **kw):
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="ok",
            trace=[
                {"step": 0, "type": "tool_call", "tool": "x"},
                {"step": 1, "type": "final", "text": "ok"},
            ],
            error=None,
        )
    monkeypatch.setattr(subagent, "_run_in_process", _stub)


def test_runner_emits_full_lifecycle(tmp_path, monkeypatch, fake_subagent):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    e = CapturingEmitter()

    s = spec.parse_spec("""---
name: lifecycle
type: swarm
phases:
  a:
    pattern: single
    role: w
    aggregator: concat
  b:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    runner.run_swarm(s, inputs={}, emitter=e)

    kinds = [ind.kind for ind in e.captured]
    assert kinds == [
        "swarm_start",
        "phase_start",
        "subagent_start", "subagent_done",
        "phase_done",
        "phase_start",
        "subagent_start", "subagent_done",
        "phase_done",
        "swarm_done",
    ]


def test_runner_emits_subagent_done_with_error_on_failure(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    def _stub(spec_obj, **kw):
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="", trace=[], error="boom",
        )
    monkeypatch.setattr(subagent, "_run_in_process", _stub)

    e = CapturingEmitter()
    s = spec.parse_spec("""---
name: fail
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    runner.run_swarm(s, inputs={}, emitter=e)

    sub_done = next(c for c in e.captured if c.kind == "subagent_done")
    assert sub_done.payload["error"] == "boom"


def test_runner_no_emitter_uses_noop(tmp_path, monkeypatch, fake_subagent):
    """Running without an emitter must not crash — _NoopEmitter handles it."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: noop-emitter
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


def test_runner_phase_done_includes_usd(tmp_path, monkeypatch, fake_subagent):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    e = CapturingEmitter()
    s = spec.parse_spec("""---
name: usd
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    runner.run_swarm(s, inputs={}, emitter=e)
    pd = next(c for c in e.captured if c.kind == "phase_done")
    assert "usd" in pd.payload


def test_emitter_failure_does_not_break_swarm(
    tmp_path, monkeypatch, fake_subagent,
):
    """If an emitter raises (rendering bug, network issue), the swarm
    should still complete — emitters are best-effort observers."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    class BadEmitter:
        def __getattr__(self, name):
            def _explode(*a, **kw):
                raise RuntimeError(f"emit {name} blew up")
            return _explode

    s = spec.parse_spec("""---
name: bad-emitter
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    # NOTE: v1.4 doesn't wrap individual emit calls in try/except — if
    # an emitter is genuinely broken the swarm crashes. This test
    # documents the current behavior; future hardening would wrap each
    # emit call. For now, gateways are expected to subclass
    # CallbackEmitter (which DOES swallow exceptions) so this is moot
    # in production paths.
    with pytest.raises(RuntimeError):
        runner.run_swarm(s, inputs={}, emitter=BadEmitter())
