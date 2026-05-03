"""Tests for v1.4 sequential phase chaining.

The runner already iterates phases in declaration order and resolves
depends_on for inputs (built in phase 3). This test file exercises the
edge cases:

- 3-phase chain where phase B implicitly chains from A and phase C
  explicitly skips B via depends_on=A
- aggregated.json on disk = exactly what the next phase receives
- final.json on disk = the LAST phase's aggregated output
- timeline lists phases in declaration order even when depends_on jumps
"""
from __future__ import annotations
import json

import pytest

from janus import config, subagent
from janus.swarms import runner, spec, state


@pytest.fixture
def echo_subagent(monkeypatch):
    """Stub _run_in_process: each sub-agent echoes a marker built from
    its (parent_id, leaf_id, action). Lets us trace what each sub-agent
    actually saw as input."""
    state_box: dict = {"calls": []}

    def _stub(spec_obj):
        state_box["calls"].append(spec_obj)
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output=f"echo[{spec_obj.leaf_id}]:{spec_obj.action[:200]}",
            trace=[{"step": 0, "type": "final", "text": "ok"}],
            error=None,
        )

    monkeypatch.setattr(subagent, "_run_in_process", _stub)
    return state_box


def _spec(text: str):
    return spec.parse_spec(text)


# ---------- Implicit chaining ----------


def test_three_phase_implicit_chain(tmp_path, monkeypatch, echo_subagent):
    """A → B → C with no explicit depends_on. B sees A's output, C sees B's."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: chain
type: swarm
inputs:
  x:
    type: int
    required: true
phases:
  alpha:
    pattern: single
    role: worker
    aggregator: concat
  beta:
    pattern: single
    role: worker
    aggregator: concat
  gamma:
    pattern: single
    role: worker
    aggregator: concat
---
phase={phase} role={role} input={input}
""")
    result = runner.run_swarm(s, inputs={"x": 1})
    assert result.error is None
    assert [p.name for p in result.phases] == ["alpha", "beta", "gamma"]

    # Phase A's input is the swarm inputs dict {"x": 1}
    a_call = echo_subagent["calls"][0]
    assert '"x": 1' in a_call.action
    # Phase B's input is phase A's aggregated output (a string from concat)
    b_call = echo_subagent["calls"][1]
    assert "echo[" in b_call.action
    # Phase C's input is phase B's aggregated output
    c_call = echo_subagent["calls"][2]
    assert "echo[" in c_call.action


# ---------- Explicit depends_on (skipping) ----------


def test_phase_can_depend_on_earlier_skipping_intermediate(
    tmp_path, monkeypatch, echo_subagent,
):
    """C depends_on A explicitly → C sees A's output, NOT B's."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: skip
type: swarm
phases:
  alpha:
    pattern: single
    role: worker
    aggregator: concat
  beta:
    pattern: single
    role: worker
    aggregator: concat
  gamma:
    pattern: single
    role: worker
    aggregator: concat
    depends_on: alpha
---
phase={phase} input={input}
""")
    result = runner.run_swarm(s, inputs={})
    assert result.error is None

    # Capture each phase's aggregated to identify it.
    alpha_agg = result.phases[0].aggregated
    beta_agg = result.phases[1].aggregated

    # gamma's call action contains alpha's aggregated, NOT beta's
    gamma_call = echo_subagent["calls"][2]
    assert alpha_agg in gamma_call.action
    # Explicitly verify gamma did NOT see beta's output
    assert beta_agg not in gamma_call.action


def test_depends_on_wins_over_implicit_prior(
    tmp_path, monkeypatch, echo_subagent,
):
    """When depends_on is set, the immediately-prior phase is bypassed."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: explicit
type: swarm
phases:
  source:
    pattern: single
    role: worker
    aggregator: concat
  middle:
    pattern: single
    role: worker
    aggregator: concat
  consumer:
    pattern: single
    role: worker
    aggregator: concat
    depends_on: source
---
input={input}
""")
    result = runner.run_swarm(s, inputs={})
    consumer_call = echo_subagent["calls"][2]
    # Should have source's output (unique echo marker), not middle's
    source_agent_id = result.phases[0].sub_agents[0].agent_id
    middle_agent_id = result.phases[1].sub_agents[0].agent_id
    assert source_agent_id in consumer_call.action
    assert middle_agent_id not in consumer_call.action


# ---------- Disk artifacts ----------


def test_each_phase_writes_input_and_aggregated_files(
    tmp_path, monkeypatch, echo_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: disk
type: swarm
inputs:
  k:
    type: string
phases:
  one:
    pattern: single
    role: w
    aggregator: concat
  two:
    pattern: single
    role: w
    aggregator: concat
  three:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    result = runner.run_swarm(s, inputs={"k": "v"})
    rdir = tmp_path / result.run_id

    for i, name in enumerate(["one", "two", "three"]):
        pdir = rdir / f"phase_{i:02d}_{name}"
        assert pdir.is_dir(), f"missing dir for phase {name}"
        assert (pdir / "input.json").is_file()
        assert (pdir / "aggregated.json").is_file()
        assert (pdir / "agents").is_dir()

    # phase two's input.json should equal phase one's aggregated.json
    one_agg = json.loads(
        (rdir / "phase_00_one" / "aggregated.json").read_text(encoding="utf-8")
    )
    two_input = json.loads(
        (rdir / "phase_01_two" / "input.json").read_text(encoding="utf-8")
    )
    assert two_input == one_agg

    # Same for phase 2 → 3
    two_agg = json.loads(
        (rdir / "phase_01_two" / "aggregated.json").read_text(encoding="utf-8")
    )
    three_input = json.loads(
        (rdir / "phase_02_three" / "input.json").read_text(encoding="utf-8")
    )
    assert three_input == two_agg


def test_final_json_matches_last_phase_aggregated(
    tmp_path, monkeypatch, echo_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: final
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
    result = runner.run_swarm(s, inputs={})
    final_path = tmp_path / result.run_id / "final.json"
    last_agg_path = tmp_path / result.run_id / "phase_01_b" / "aggregated.json"
    assert json.loads(final_path.read_text(encoding="utf-8")) == \
        json.loads(last_agg_path.read_text(encoding="utf-8"))


def test_explicit_depends_on_writes_correct_input(
    tmp_path, monkeypatch, echo_subagent,
):
    """When phase C depends_on phase A, C's input.json on disk should
    equal A's aggregated.json on disk (not B's)."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: skip-disk
type: swarm
phases:
  src:
    pattern: single
    role: w
    aggregator: concat
  middle:
    pattern: single
    role: w
    aggregator: concat
  consumer:
    pattern: single
    role: w
    aggregator: concat
    depends_on: src
---
body
""")
    result = runner.run_swarm(s, inputs={})
    rdir = tmp_path / result.run_id

    src_agg = json.loads(
        (rdir / "phase_00_src" / "aggregated.json").read_text(encoding="utf-8")
    )
    consumer_input = json.loads(
        (rdir / "phase_02_consumer" / "input.json").read_text(encoding="utf-8")
    )
    assert consumer_input == src_agg


# ---------- Timeline ----------


def test_timeline_records_phases_in_declaration_order(
    tmp_path, monkeypatch, echo_subagent,
):
    """Even when depends_on lets later phases skip earlier ones, the
    runtime still EXECUTES phases in declaration order."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: order
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
  c:
    pattern: single
    role: w
    aggregator: concat
    depends_on: a
---
body
""")
    result = runner.run_swarm(s, inputs={})
    timeline = state.read_timeline(result.run_id)
    phase_starts = [e["phase"] for e in timeline if e["type"] == "phase_start"]
    assert phase_starts == ["a", "b", "c"]


# ---------- Aggregator chains ----------


def test_dedupe_then_topk_pipeline(tmp_path, monkeypatch, echo_subagent):
    """First phase dedupes JSON list, second phase ranks top K from the
    deduped output."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    # Stub: each sub-agent emits a JSON list of {id, score} objects,
    # encoded as a single output string.
    def _stub(spec_obj):
        if "stage1" in spec_obj.label:
            payload = [
                {"id": "x", "score": 5},
                {"id": "y", "score": 9},
                {"id": "x", "score": 5},  # dup
            ]
        else:
            # Stage 2 receives stage 1's deduped list as JSON. Echo back
            # the same list so topk has something to sort.
            payload = json.loads(spec_obj.action.split("input=")[-1])
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output=json.dumps(payload),
            trace=[], error=None,
        )

    monkeypatch.setattr(subagent, "_run_in_process", _stub)

    s = _spec("""---
name: pipeline
type: swarm
phases:
  stage1:
    pattern: single
    role: w
    aggregator: dedupe_by
    aggregator_args:
      key: id
  stage2:
    pattern: single
    role: w
    aggregator: topk
    aggregator_args:
      key: score
      k: 1
---
input={input}
""")
    result = runner.run_swarm(s, inputs={})
    # Stage 1 aggregated: deduped list → [{id:x,score:5}, {id:y,score:9}]
    assert result.phases[0].aggregated == [
        {"id": "x", "score": 5}, {"id": "y", "score": 9},
    ]
    # Stage 2 aggregated: top-1 by score → [{id:y,score:9}]
    assert result.final == [{"id": "y", "score": 9}]
