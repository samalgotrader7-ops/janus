"""Tests for v1.4 swarm lifecycle hooks.

Four new events extend hooks.ALL_EVENTS:
  - PreSwarmSpawn        (deny aborts whole swarm)
  - PostSwarmComplete    (observation)
  - PreSubagentSpawn     (deny skips that one sub-agent)
  - PostSubagentComplete (observation)

Hook commands receive {event, payload} JSON on stdin; return decision
JSON on stdout. We test by monkeypatching hooks.fire to record events
without spawning real subprocesses.
"""
from __future__ import annotations
import json

import pytest

from janus import config, hooks, subagent
from janus.swarms import runner, spec


# ---------- ALL_EVENTS extended ----------


def test_all_events_includes_swarm_lifecycle():
    assert hooks.PRE_SWARM_SPAWN in hooks.ALL_EVENTS
    assert hooks.POST_SWARM_COMPLETE in hooks.ALL_EVENTS
    assert hooks.PRE_SUBAGENT_SPAWN in hooks.ALL_EVENTS
    assert hooks.POST_SUBAGENT_COMPLETE in hooks.ALL_EVENTS


def test_load_hooks_includes_new_events_in_index(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOOKS_FILE", tmp_path / "hooks.json")
    monkeypatch.setattr(config, "HOOKS_DIR", tmp_path / "hooks")
    idx = hooks.load_hooks()
    assert hooks.PRE_SWARM_SPAWN in idx
    assert idx[hooks.PRE_SWARM_SPAWN] == []


def test_hooks_json_can_register_pre_swarm_spawn(tmp_path, monkeypatch):
    """A user-authored hooks.json with a PreSwarmSpawn entry should load."""
    monkeypatch.setattr(config, "HOOKS_FILE", tmp_path / "hooks.json")
    monkeypatch.setattr(config, "HOOKS_DIR", tmp_path / "hooks-empty")
    config.HOOKS_FILE.write_text(json.dumps({
        "hooks": {
            "PreSwarmSpawn": [
                {"command": "echo {}", "matcher": "demo-spec"},
            ],
        },
    }), encoding="utf-8")
    idx = hooks.load_hooks()
    pre = idx[hooks.PRE_SWARM_SPAWN]
    assert len(pre) == 1
    assert pre[0].matcher == "demo-spec"


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


@pytest.fixture
def captured_hooks(monkeypatch):
    """Replace hooks.fire so we can record every event the runner emits
    without spawning shell subprocesses."""
    fires: list[dict] = []
    decisions: dict = {}  # event_name → HookDecision to return

    def fake_fire(event, payload, *, match_field=None, hooks_index=None):
        fires.append({
            "event": event, "payload": payload, "match_field": match_field,
        })
        if event in decisions:
            return decisions[event]
        return hooks.HookDecision()  # allow

    monkeypatch.setattr(hooks, "fire", fake_fire)
    return {"fires": fires, "decisions": decisions}


def _spec(text: str):
    return spec.parse_spec(text)


# ---------- PreSwarmSpawn ----------


def test_pre_swarm_spawn_fires_with_payload(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: pre-fire
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

    pre = [f for f in captured_hooks["fires"] if f["event"] == hooks.PRE_SWARM_SPAWN]
    assert len(pre) == 1
    payload = pre[0]["payload"]
    assert payload["spec"] == "pre-fire"
    assert payload["spec_version"] == 1
    assert pre[0]["match_field"] == "spec"


def test_pre_swarm_spawn_deny_aborts_swarm(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    captured_hooks["decisions"][hooks.PRE_SWARM_SPAWN] = hooks.HookDecision(
        allow=False, reason="not allowed by policy",
    )

    s = _spec("""---
name: blocked
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
    assert result.error and "hook_denied" in result.error
    assert "not allowed by policy" in result.error
    # No sub-agent dispatched.
    assert fake_subagent["calls"] == 0
    # No run dir created either.
    assert list(tmp_path.iterdir()) == []


# ---------- PostSwarmComplete ----------


def test_post_swarm_complete_fires_with_summary(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: post
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
    runner.run_swarm(s, inputs={})
    post = [f for f in captured_hooks["fires"] if f["event"] == hooks.POST_SWARM_COMPLETE]
    assert len(post) == 1
    payload = post[0]["payload"]
    assert payload["spec"] == "post"
    assert payload["n_phases"] == 2
    assert payload["n_subagents_total"] == 2
    assert payload["n_errors_total"] == 0
    assert "budget_snapshot" in payload


def test_post_swarm_complete_does_not_fire_when_pre_denied(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    """Symmetric: PostSwarmComplete only fires on swarms that actually ran."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    captured_hooks["decisions"][hooks.PRE_SWARM_SPAWN] = hooks.HookDecision(
        allow=False, reason="nope",
    )
    s = _spec("""---
name: denied
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
    post = [f for f in captured_hooks["fires"] if f["event"] == hooks.POST_SWARM_COMPLETE]
    assert post == []


# ---------- PreSubagentSpawn ----------


def test_pre_subagent_spawn_fires_per_dispatch(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: pre-sub
type: swarm
phases:
  a:
    pattern: single
    role: alpha
    aggregator: concat
  b:
    pattern: single
    role: beta
    aggregator: concat
---
body
""")
    runner.run_swarm(s, inputs={})
    pre_sub = [
        f for f in captured_hooks["fires"]
        if f["event"] == hooks.PRE_SUBAGENT_SPAWN
    ]
    assert len(pre_sub) == 2
    roles = [f["payload"]["role"] for f in pre_sub]
    assert roles == ["alpha", "beta"]
    # match_field is role so users can scope hooks per-role
    assert pre_sub[0]["match_field"] == "role"


def test_pre_subagent_spawn_deny_skips_subagent(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    captured_hooks["decisions"][hooks.PRE_SUBAGENT_SPAWN] = hooks.HookDecision(
        allow=False, reason="role not approved",
    )
    s = _spec("""---
name: skip-sub
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
    # Sub-agent NOT dispatched (stub call count stays 0)
    assert fake_subagent["calls"] == 0
    # But the swarm itself didn't crash; sub-agent just recorded as denied
    assert result.error is None
    sub = result.phases[0].sub_agents[0]
    assert sub.error and "hook_denied" in sub.error


def test_pre_subagent_spawn_payload_includes_model_and_phase(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: payload
type: swarm
phases:
  go:
    pattern: single
    role: w
    model: cheap-haiku
    aggregator: concat
---
body
""")
    runner.run_swarm(s, inputs={})
    pre_sub = next(
        f for f in captured_hooks["fires"]
        if f["event"] == hooks.PRE_SUBAGENT_SPAWN
    )
    assert pre_sub["payload"]["model"] == "cheap-haiku"
    assert pre_sub["payload"]["phase"] == "go"


# ---------- PostSubagentComplete ----------


def test_post_subagent_complete_fires_per_subagent(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: post-sub
type: swarm
phases:
  a:
    pattern: single
    role: alpha
    aggregator: concat
  b:
    pattern: single
    role: beta
    aggregator: concat
---
body
""")
    runner.run_swarm(s, inputs={})
    post_sub = [
        f for f in captured_hooks["fires"]
        if f["event"] == hooks.POST_SUBAGENT_COMPLETE
    ]
    assert len(post_sub) == 2
    roles = [f["payload"]["role"] for f in post_sub]
    assert sorted(roles) == ["alpha", "beta"]
    # Each post payload includes summary fields
    for f in post_sub:
        assert "output_chars" in f["payload"]
        assert "tool_calls" in f["payload"]
        assert "error" in f["payload"]


def test_post_subagent_complete_records_error(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    def _stub(spec_obj, **kw):
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="", trace=[], error="boom",
        )
    monkeypatch.setattr(subagent, "_run_in_process", _stub)

    s = _spec("""---
name: post-err
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
    post_sub = next(
        f for f in captured_hooks["fires"]
        if f["event"] == hooks.POST_SUBAGENT_COMPLETE
    )
    assert post_sub["payload"]["error"] == "boom"


# ---------- Order of events ----------


def test_events_fire_in_lifecycle_order(
    tmp_path, monkeypatch, fake_subagent, captured_hooks,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = _spec("""---
name: order
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
    swarm_events = [
        f["event"] for f in captured_hooks["fires"]
        if f["event"] in (
            hooks.PRE_SWARM_SPAWN, hooks.POST_SWARM_COMPLETE,
            hooks.PRE_SUBAGENT_SPAWN, hooks.POST_SUBAGENT_COMPLETE,
        )
    ]
    assert swarm_events == [
        hooks.PRE_SWARM_SPAWN,
        hooks.PRE_SUBAGENT_SPAWN,
        hooks.POST_SUBAGENT_COMPLETE,
        hooks.POST_SWARM_COMPLETE,
    ]
