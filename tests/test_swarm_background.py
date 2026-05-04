"""Tests for v1.5 phase 7: background swarm mode.

Tests the parent-side wiring (subprocess.Popen with detachment) using
mocks, plus the runner's run_id_override path. The actual cross-process
spawn isn't exercised here (would require integration tests with real
processes); we trust subprocess.Popen behavior and test our usage of it.
"""
from __future__ import annotations
import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest

from janus import __main__ as main_mod
from janus import config, subagent
from janus.swarms import recursion, runner, spec, state


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


# ---------- run_id_override ----------


def test_runner_uses_provided_run_id(isolated_dirs, fake_subagent):
    """When run_id_override is passed, runner uses it instead of minting."""
    _write_demo_spec(isolated_dirs / "specs", "demo")
    s = spec.find_spec("demo")
    custom_id = "swarm-2026-05-04T10-00-00-aaaa"
    state.init_run_dir(custom_id)  # Pre-create as background mode would
    result = runner.run_swarm(s, inputs={}, run_id_override=custom_id)
    assert result.run_id == custom_id


def test_runner_mints_id_when_no_override(isolated_dirs, fake_subagent):
    _write_demo_spec(isolated_dirs / "specs", "demo")
    s = spec.find_spec("demo")
    result = runner.run_swarm(s, inputs={})
    assert result.run_id.startswith("swarm-")


def test_run_id_override_works_end_to_end(isolated_dirs, fake_subagent):
    """Pre-mint + write metadata + run with override → metadata persists."""
    _write_demo_spec(isolated_dirs / "specs", "demo")
    s = spec.find_spec("demo")

    pre_id = state.new_run_id()
    state.init_run_dir(pre_id)
    state.write_metadata(pre_id, {"spec_name": "demo", "background": True})

    result = runner.run_swarm(s, inputs={}, run_id_override=pre_id)
    assert result.run_id == pre_id

    # Metadata pre-write was preserved (write_metadata in runner overwrites
    # but should still mention the spec).
    meta = state.read_metadata(pre_id)
    assert meta["spec_name"] == "demo"


# ---------- Background spawn parent ----------


def _capture(monkeypatch, fn, *args, **kw):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    code = 0
    try:
        fn(*args, **kw)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


def test_background_run_unknown_spec_exits(monkeypatch, isolated_dirs):
    out, code = _capture(
        monkeypatch, main_mod._swarm_run_background, "nope", [],
    )
    assert code == 2
    assert "no spec named" in out


def test_background_run_invalid_inputs_exits(monkeypatch, isolated_dirs):
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
    out, code = _capture(
        monkeypatch, main_mod._swarm_run_background, "withinput", [],
    )
    assert code == 2
    assert "missing required input" in out


def test_background_run_spawns_child_and_returns(
    monkeypatch, isolated_dirs,
):
    """Mock subprocess.Popen — verify parent pre-mints id, writes
    metadata, spawns the right command, and prints the run_id."""
    _write_demo_spec(isolated_dirs / "specs", "demo")

    captured: dict = {}

    class FakeProc:
        pid = 12345

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(
        "subprocess.Popen", fake_popen,
    )

    out, code = _capture(
        monkeypatch, main_mod._swarm_run_background, "demo", [],
    )
    assert code == 0
    assert "swarm spawned in background" in out
    assert "run_id:" in out
    assert "12345" in out  # the fake pid

    # Verify the child cmd looks right
    cmd = captured["cmd"]
    assert cmd[0] == sys.executable
    assert "_bg_run" in cmd
    # Inputs serialized as JSON
    assert "{}" in cmd  # empty inputs dict


def test_background_run_writes_metadata_pre_spawn(
    monkeypatch, isolated_dirs,
):
    """Metadata is on disk BEFORE the child starts so status queries work
    immediately after the spawn returns."""
    _write_demo_spec(isolated_dirs / "specs", "demo")

    runs_seen: list = []

    def fake_popen(cmd, **kw):
        # By now the metadata should be on disk
        runs_dir = isolated_dirs / "runs"
        for d in runs_dir.iterdir():
            if d.is_dir():
                meta_path = d / "metadata.json"
                if meta_path.is_file():
                    runs_seen.append(json.loads(meta_path.read_text()))

        class P:
            pid = 1
        return P()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    _capture(monkeypatch, main_mod._swarm_run_background, "demo", [])

    assert len(runs_seen) == 1
    assert runs_seen[0].get("background") is True
    assert runs_seen[0].get("spec_name") == "demo"


def test_background_run_writes_pid_file(monkeypatch, isolated_dirs):
    _write_demo_spec(isolated_dirs / "specs", "demo")

    class FakeProc:
        pid = 99999

    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **kw: FakeProc(),
    )
    _capture(monkeypatch, main_mod._swarm_run_background, "demo", [])

    runs_dir = isolated_dirs / "runs"
    pid_files = list(runs_dir.glob("*/pid"))
    assert len(pid_files) == 1
    assert pid_files[0].read_text() == "99999"


def test_background_run_appends_timeline(monkeypatch, isolated_dirs):
    _write_demo_spec(isolated_dirs / "specs", "demo")

    class FakeProc:
        pid = 1
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: FakeProc())

    _capture(monkeypatch, main_mod._swarm_run_background, "demo", [])

    runs_dir = isolated_dirs / "runs"
    tl_files = list(runs_dir.glob("*/timeline.jsonl"))
    assert len(tl_files) == 1
    lines = tl_files[0].read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    assert any(e["type"] == "background_spawn" for e in events)


def test_background_run_spawn_failure_exits(monkeypatch, isolated_dirs):
    _write_demo_spec(isolated_dirs / "specs", "demo")

    def boom(*a, **kw):
        raise OSError("Cannot allocate child")

    monkeypatch.setattr("subprocess.Popen", boom)
    out, code = _capture(
        monkeypatch, main_mod._swarm_run_background, "demo", [],
    )
    assert code == 1
    assert "failed to spawn child" in out


# ---------- Background child entry ----------


def test_bg_child_runs_swarm_with_provided_id(
    monkeypatch, isolated_dirs, fake_subagent,
):
    """The _bg_run subcommand entry-point invokes runner with the parent's
    pre-minted run_id."""
    _write_demo_spec(isolated_dirs / "specs", "demo")
    pre_id = "swarm-2026-05-04T10-00-00-bbbb"
    state.init_run_dir(pre_id)
    state.write_metadata(pre_id, {"spec_name": "demo", "background": True})

    # _swarm_bg_child calls sys.exit at the end; catch it
    with pytest.raises(SystemExit) as ei:
        main_mod._swarm_bg_child(pre_id, "demo", "{}")
    assert ei.value.code == 0

    # Verify final.json was written under the pre-minted id
    final = state.read_final(pre_id)
    assert final is not None


def test_bg_child_with_bad_inputs_exits(monkeypatch, isolated_dirs):
    _write_demo_spec(isolated_dirs / "specs", "demo")
    with pytest.raises(SystemExit) as ei:
        main_mod._swarm_bg_child("rid", "demo", "not json")
    assert ei.value.code == 2


def test_bg_child_with_unknown_spec_exits(monkeypatch, isolated_dirs):
    with pytest.raises(SystemExit) as ei:
        main_mod._swarm_bg_child("rid", "nonexistent", "{}")
    assert ei.value.code == 2


def test_bg_child_writes_crash_to_final_json(
    monkeypatch, isolated_dirs,
):
    """If the runner raises (e.g., infrastructure failure), the child
    writes final.json with the crash info so status queries surface it."""
    _write_demo_spec(isolated_dirs / "specs", "demo")
    pre_id = "swarm-test-crash"
    state.init_run_dir(pre_id)

    def boom(*a, **kw):
        raise RuntimeError("infra exploded")

    monkeypatch.setattr(runner, "run_swarm", boom)

    with pytest.raises(SystemExit) as ei:
        main_mod._swarm_bg_child(pre_id, "demo", "{}")
    assert ei.value.code == 1

    final = state.read_final(pre_id)
    assert final["error"]
    assert "infra exploded" in final["error"]
