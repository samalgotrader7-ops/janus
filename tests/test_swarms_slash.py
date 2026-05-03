"""Tests for v1.4 /swarm slash command (the shared text dispatcher)."""
from __future__ import annotations

import pytest

from janus import config, subagent
from janus.swarms import runner, slash, spec, state


@pytest.fixture
def fake_subagent(monkeypatch):
    def _stub(spec_obj, **kw):
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="ok", trace=[], error=None,
        )
    monkeypatch.setattr(subagent, "_run_in_process", _stub)


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", tmp_path / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path / "runs")
    (tmp_path / "specs").mkdir()
    (tmp_path / "runs").mkdir()
    return tmp_path


def _write_spec(specs_dir, name="demo"):
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
body
""", encoding="utf-8")


# ---------- Help ----------


def test_handle_empty_arg_shows_help(isolated_dirs):
    out = slash.handle("")
    assert "/swarm" in out
    assert "list" in out
    assert "describe" in out


def test_handle_help_subcommand(isolated_dirs):
    assert "list" in slash.handle("help")
    assert "list" in slash.handle("--help")


def test_handle_unknown_subcommand_includes_help(isolated_dirs):
    out = slash.handle("nonsense")
    assert "unknown" in out
    assert "list" in out


# ---------- list ----------


def test_handle_list_empty(isolated_dirs):
    out = slash.handle("list")
    assert "specs:" in out
    assert "(none" in out


def test_handle_list_with_specs(isolated_dirs):
    _write_spec(isolated_dirs / "specs", "demo")
    out = slash.handle("list")
    assert "demo" in out
    assert "test spec" in out


# ---------- describe ----------


def test_handle_describe_missing_arg(isolated_dirs):
    out = slash.handle("describe")
    assert "usage" in out


def test_handle_describe_unknown(isolated_dirs):
    out = slash.handle("describe nope")
    assert "no spec named" in out


def test_handle_describe_renders(isolated_dirs):
    _write_spec(isolated_dirs / "specs", "demo")
    out = slash.handle("describe demo")
    assert "name:" in out
    assert "demo" in out
    assert "phases" in out


# ---------- run ----------


def test_handle_run_missing_arg(isolated_dirs):
    out = slash.handle("run")
    assert "usage" in out


def test_handle_run_unknown_spec(isolated_dirs):
    out = slash.handle("run nope")
    assert "no spec named" in out


def test_handle_run_basic(isolated_dirs, fake_subagent):
    _write_spec(isolated_dirs / "specs", "demo")
    out = slash.handle("run demo")
    assert "run_id:" in out
    assert "phases: 1" in out


def test_handle_run_with_kv_args(isolated_dirs, fake_subagent):
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
    out = slash.handle("run withinput count=5")
    assert "run_id:" in out


def test_handle_run_bad_input_returns_error(isolated_dirs, fake_subagent):
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
    out = slash.handle("run withinput")
    assert "spec error" in out
    assert "missing required input" in out


def test_handle_run_malformed_kv(isolated_dirs, fake_subagent):
    _write_spec(isolated_dirs / "specs", "demo")
    out = slash.handle("run demo notakvpair")
    # demo has no inputs, so the unknown extra fails validation, but we
    # also test the parse-error path separately:
    assert "error" in out.lower() or "unknown" in out.lower()


# ---------- status ----------


def test_handle_status_missing_arg(isolated_dirs):
    out = slash.handle("status")
    assert "usage" in out


def test_handle_status_unknown_run(isolated_dirs):
    out = slash.handle("status swarm-fake")
    assert "no such run" in out


def test_handle_status_complete(isolated_dirs, fake_subagent):
    _write_spec(isolated_dirs / "specs", "demo")
    s = spec.find_spec("demo")
    result = runner.run_swarm(s, inputs={})
    out = slash.handle(f"status {result.run_id}")
    assert "COMPLETE" in out
    assert result.run_id in out


def test_handle_status_cancelled(isolated_dirs):
    rid = "swarm-2026-05-03T10-00-00-aaaa"
    state.init_run_dir(rid)
    state.write_metadata(rid, {"spec_name": "x", "started": "now"})
    state.write_cancel_flag(rid)
    out = slash.handle(f"status {rid}")
    assert "CANCELLED" in out


# ---------- cancel ----------


def test_handle_cancel_missing_arg(isolated_dirs):
    out = slash.handle("cancel")
    assert "usage" in out


def test_handle_cancel_unknown_run(isolated_dirs):
    out = slash.handle("cancel swarm-fake")
    assert "no such run" in out


def test_handle_cancel_writes_flag(isolated_dirs):
    rid = "swarm-2026-05-03T10-00-00-aaaa"
    state.init_run_dir(rid)
    state.write_metadata(rid, {"spec_name": "x", "started": "now"})
    out = slash.handle(f"cancel {rid}")
    assert "cancellation flag written" in out
    assert state.is_cancelled(rid)


# ---------- _parse_kv ----------


def test_parse_kv_simple():
    out = slash._parse_kv(["x=1", "y=hi"])
    assert out == {"x": 1, "y": "hi"}


def test_parse_kv_json_values():
    out = slash._parse_kv(['list=[1,2]', 'x=true'])
    assert out == {"list": [1, 2], "x": True}


def test_parse_kv_rejects_bare():
    with pytest.raises(ValueError):
        slash._parse_kv(["nokey"])
