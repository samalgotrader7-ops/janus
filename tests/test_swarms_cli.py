"""Tests for v1.4 `janus swarm <subcommand>` CLI."""
from __future__ import annotations
import json
import sys
from io import StringIO

import pytest

from janus import __main__ as main_mod
from janus import config, subagent
from janus.swarms import runner, spec, state


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
    """Point all swarms paths at a temp dir."""
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", tmp_path / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path / "runs")
    (tmp_path / "specs").mkdir()
    (tmp_path / "runs").mkdir()
    return tmp_path


def _write_spec(specs_dir, name: str, body: str = ""):
    p = specs_dir / f"{name}.md"
    p.write_text(f"""---
name: {name}
type: swarm
description: test spec
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
{body}
""", encoding="utf-8")
    return p


def _capture_stdout(monkeypatch, fn, *args, **kw):
    """Run fn and return its stdout. Catches SystemExit and returns
    (output, exit_code) so tests can assert on either."""
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    code = 0
    try:
        fn(*args, **kw)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


# ---------- _parse_kv_args ----------


def test_parse_kv_args_simple():
    out = main_mod._parse_kv_args(["x=1", "y=hi"])
    assert out == {"x": 1, "y": "hi"}


def test_parse_kv_args_json_values():
    out = main_mod._parse_kv_args(['list=[1,2,3]', 'obj={"a":1}'])
    assert out == {"list": [1, 2, 3], "obj": {"a": 1}}


def test_parse_kv_args_string_fallback():
    """Non-JSON-decodable values pass through as strings."""
    out = main_mod._parse_kv_args(["name=Sam"])
    assert out == {"name": "Sam"}


def test_parse_kv_args_rejects_bare_token(monkeypatch):
    out, code = _capture_stdout(monkeypatch, main_mod._parse_kv_args, ["nokeyhere"])
    assert code == 2


# ---------- list ----------


def test_swarm_list_empty(monkeypatch, isolated_dirs):
    out, _ = _capture_stdout(monkeypatch, main_mod._swarm_list)
    assert "specs:" in out
    assert "(none" in out
    assert "recent runs" in out


def test_swarm_list_shows_specs(monkeypatch, isolated_dirs):
    _write_spec(isolated_dirs / "specs", "demo")
    out, _ = _capture_stdout(monkeypatch, main_mod._swarm_list)
    assert "demo" in out
    assert "test spec" in out


def test_swarm_list_shows_recent_runs(monkeypatch, isolated_dirs):
    rdir = isolated_dirs / "runs" / "swarm-2026-05-03T10-00-00-aaaa"
    rdir.mkdir(parents=True)
    state.write_metadata("swarm-2026-05-03T10-00-00-aaaa", {
        "spec_name": "old-run", "started": "now",
    })
    out, _ = _capture_stdout(monkeypatch, main_mod._swarm_list)
    assert "swarm-2026-05-03T10-00-00-aaaa" in out
    assert "old-run" in out


# ---------- describe ----------


def test_swarm_describe_unknown_spec_exits(monkeypatch, isolated_dirs):
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_describe, "nope")
    assert "no spec named" in out
    assert code == 2


def test_swarm_describe_renders_spec(monkeypatch, isolated_dirs):
    _write_spec(isolated_dirs / "specs", "demo", body="instruction body")
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_describe, "demo")
    assert code == 0
    assert "name:" in out
    assert "demo" in out
    assert "phases (1)" in out
    assert "go" in out


# ---------- run ----------


def test_swarm_run_invokes_runner(
    monkeypatch, isolated_dirs, fake_subagent,
):
    _write_spec(isolated_dirs / "specs", "demo")
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_run, "demo", [])
    assert code == 0
    assert "run_id:" in out
    assert "phases: 1" in out


def test_swarm_run_unknown_spec_exits(monkeypatch, isolated_dirs):
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_run, "nope", [])
    assert code == 2


def test_swarm_run_passes_kv_args(
    monkeypatch, isolated_dirs, fake_subagent,
):
    """k=v arguments → spec.validate_inputs."""
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
    out, code = _capture_stdout(
        monkeypatch, main_mod._swarm_run, "withinput", ["count=5"],
    )
    assert code == 0
    assert "run_id:" in out


def test_swarm_run_bad_input_surfaces_spec_error(
    monkeypatch, isolated_dirs, fake_subagent,
):
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
    out, code = _capture_stdout(
        monkeypatch, main_mod._swarm_run, "withinput", [],  # missing count
    )
    assert "spec error" in out
    assert "missing required input" in out
    assert code == 2


# ---------- status ----------


def test_swarm_status_unknown_run_exits(monkeypatch, isolated_dirs):
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_status, "swarm-fake")
    assert code == 2


def test_swarm_status_complete(monkeypatch, isolated_dirs, fake_subagent):
    _write_spec(isolated_dirs / "specs", "demo")
    s = spec.find_spec("demo")
    result = runner.run_swarm(s, inputs={})
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_status, result.run_id)
    assert code == 0
    assert "COMPLETE" in out
    assert result.run_id in out


def test_swarm_status_running_when_no_final(monkeypatch, isolated_dirs):
    """Hand-craft a run dir with metadata but no final.json."""
    rid = "swarm-2026-05-03T10-00-00-aaaa"
    state.init_run_dir(rid)
    state.write_metadata(rid, {"spec_name": "x", "started": "now"})
    state.append_timeline(rid, {"type": "phase_start", "phase": "p"})
    out, _ = _capture_stdout(monkeypatch, main_mod._swarm_status, rid)
    assert "RUNNING" in out


def test_swarm_status_cancelled(monkeypatch, isolated_dirs):
    rid = "swarm-2026-05-03T10-00-00-aaaa"
    state.init_run_dir(rid)
    state.write_metadata(rid, {"spec_name": "x", "started": "now"})
    state.write_cancel_flag(rid)
    out, _ = _capture_stdout(monkeypatch, main_mod._swarm_status, rid)
    assert "CANCELLED" in out


# ---------- cancel ----------


def test_swarm_cancel_writes_flag(monkeypatch, isolated_dirs):
    rid = "swarm-2026-05-03T10-00-00-aaaa"
    state.init_run_dir(rid)
    state.write_metadata(rid, {"spec_name": "x", "started": "now"})
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_cancel, rid)
    assert code == 0
    assert state.is_cancelled(rid)
    assert "cancellation flag written" in out


def test_swarm_cancel_unknown_run_exits(monkeypatch, isolated_dirs):
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_cancel, "swarm-fake")
    assert code == 2


# ---------- cost ----------


def test_swarm_cost_renders(monkeypatch, isolated_dirs, fake_subagent):
    _write_spec(isolated_dirs / "specs", "demo")
    s = spec.find_spec("demo")
    result = runner.run_swarm(s, inputs={})
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_cost, result.run_id)
    assert code == 0
    assert result.run_id in out


def test_swarm_cost_unknown_run_exits(monkeypatch, isolated_dirs):
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_cost, "swarm-fake")
    assert code == 2


# ---------- trace ----------


def test_swarm_trace_renders(monkeypatch, isolated_dirs, fake_subagent):
    _write_spec(isolated_dirs / "specs", "demo")
    s = spec.find_spec("demo")
    result = runner.run_swarm(s, inputs={})
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_trace, result.run_id)
    assert code == 0
    assert "swarm_start" in out
    assert "swarm_done" in out


def test_swarm_trace_unknown_run_exits(monkeypatch, isolated_dirs):
    out, code = _capture_stdout(monkeypatch, main_mod._swarm_trace, "swarm-fake")
    assert code == 2


# ---------- Dispatcher ----------


def test_run_swarm_cli_no_args_prints_help(monkeypatch, isolated_dirs):
    monkeypatch.setattr(config, "API_KEY", "x")  # so assert_configured passes
    out, _ = _capture_stdout(monkeypatch, main_mod._run_swarm_cli, [])
    assert "janus swarm" in out
    assert "list" in out
    assert "describe" in out
    assert "run" in out


def test_run_swarm_cli_unknown_subcommand_exits(monkeypatch, isolated_dirs):
    monkeypatch.setattr(config, "API_KEY", "x")
    out, code = _capture_stdout(
        monkeypatch, main_mod._run_swarm_cli, ["nonsense"],
    )
    assert "unknown swarm subcommand" in out
    assert code == 2
