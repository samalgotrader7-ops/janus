"""Tests for v1.30.2 — built-in memory consolidation cron.

The v1.18 design intentionally had no built-in cadence — the
docstring on memory_consolidate.run_once told users to wire
agent_create() if they wanted automation. v1.30.2 adds an env-
var-controlled cadence inside the existing daemon loop so users
who'd rather flip a switch don't have to wire a meta-skill.

DESIGN INVARIANTS PINNED HERE:
  * OFF by default. JANUS_MEMORY_CONSOLIDATE_HOURS=0 → tick is
    a no-op (fired=False, reason="disabled").
  * State persists across daemon restarts via JSON file under
    ~/.janus/. P5: plain-text persistent state.
  * last_run_ts is marked BEFORE the LLM call so a hang/crash
    can't loop on retry; the next attempt waits the full
    interval.
  * tick() never raises — always returns a status dict.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

from janus import config, memory_consolidate_cron


# ============================================================
# should_run_now — pure-compute scheduler
# ============================================================


def test_disabled_returns_false_no_state():
    assert memory_consolidate_cron.should_run_now(
        {}, hours=0,
    ) is False


def test_disabled_returns_false_with_state():
    """Even with a stale last_run, hours=0 disables firing."""
    state = {"last_run_ts": 1.0}
    assert memory_consolidate_cron.should_run_now(
        state, hours=0, now=10000.0,
    ) is False


def test_first_run_fires_immediately_when_enabled():
    """No previous run → True (operator just enabled the cron)."""
    assert memory_consolidate_cron.should_run_now(
        {}, hours=24, now=time.time(),
    ) is True


def test_within_interval_does_not_fire():
    state = {"last_run_ts": 1000.0}
    # 30 seconds after a 1-hour interval → not due
    assert memory_consolidate_cron.should_run_now(
        state, hours=1, now=1030.0,
    ) is False


def test_at_interval_fires():
    state = {"last_run_ts": 1000.0}
    assert memory_consolidate_cron.should_run_now(
        state, hours=1, now=1000.0 + 3600,
    ) is True


def test_long_past_interval_fires():
    state = {"last_run_ts": 1000.0}
    assert memory_consolidate_cron.should_run_now(
        state, hours=24, now=1000.0 + 7 * 86400,
    ) is True


def test_corrupt_last_run_treated_as_first_run():
    """Defensive: state file may be corrupt or hand-edited."""
    state = {"last_run_ts": "yesterday"}  # not a number
    assert memory_consolidate_cron.should_run_now(
        state, hours=24, now=time.time(),
    ) is True


# ============================================================
# read_state / write_state — state file round-trip
# ============================================================


def test_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    state = {"last_run_ts": 1234.5, "runs": 3}
    memory_consolidate_cron.write_state(state)
    loaded = memory_consolidate_cron.read_state()
    assert loaded == state


def test_read_state_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert memory_consolidate_cron.read_state() == {}


def test_read_state_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    p = memory_consolidate_cron.state_path()
    p.write_text("not valid json {{{", encoding="utf-8")
    # Must NOT raise — daemon would crash on every poll.
    assert memory_consolidate_cron.read_state() == {}


def test_write_state_creates_parent_dir(tmp_path, monkeypatch):
    sub = tmp_path / "deep" / "nested"
    monkeypatch.setattr(config, "HOME", sub)
    memory_consolidate_cron.write_state({"runs": 1})
    assert (sub / "memory_consolidate_state.json").is_file()


# ============================================================
# tick() — daemon entry point
# ============================================================


def test_tick_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATE_HOURS", 0)
    out = memory_consolidate_cron.tick()
    assert out == {"fired": False, "reason": "disabled"}
    # No state file written for a no-op.
    assert not memory_consolidate_cron.state_path().exists()


def test_tick_not_due_returns_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    memory_consolidate_cron.write_state({"last_run_ts": 1000.0})
    out = memory_consolidate_cron.tick(hours=1, now=1030.0)
    assert out["fired"] is False
    assert out["reason"] == "not_due"
    assert out["wait_seconds"] > 0


def test_tick_due_fires_run_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)

    fired_with: list[bool] = []

    def fake_run_once(*, max_input_cards: int = 200):
        fired_with.append(False)
        return {"examined": 17, "written": 2}

    import janus.memory_consolidate as mc
    monkeypatch.setattr(mc, "run_once", fake_run_once)

    out = memory_consolidate_cron.tick(
        hours=1, multi_stage=False, now=time.time(),
    )
    assert out["fired"] is True
    assert out["examined"] == 17
    assert out["written"] == 2
    assert out["multi_stage"] is False
    state = memory_consolidate_cron.read_state()
    assert state["runs"] == 1
    assert state["last_examined"] == 17
    assert state["last_written"] == 2


def test_tick_due_fires_multi_stage_when_flag_set(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    calls: list[str] = []

    def fake_multi(*, max_input_cards: int = 200):
        calls.append("multi")
        return {"examined": 5, "written": 0}

    def fake_single(*, max_input_cards: int = 200):
        calls.append("single")
        return {"examined": 5, "written": 0}

    import janus.memory_consolidate as mc
    monkeypatch.setattr(mc, "run_multi_stage", fake_multi)
    monkeypatch.setattr(mc, "run_once", fake_single)

    out = memory_consolidate_cron.tick(
        hours=1, multi_stage=True, now=time.time(),
    )
    assert out["fired"] is True
    assert out["multi_stage"] is True
    assert calls == ["multi"]


def test_tick_marks_last_run_before_llm_call(tmp_path, monkeypatch):
    """Crash during run_once must NOT reset last_run — otherwise the
    next poll will retry tightly and burn budget."""
    monkeypatch.setattr(config, "HOME", tmp_path)

    def fake_crash(*, max_input_cards: int = 200):
        raise RuntimeError("model endpoint down")

    import janus.memory_consolidate as mc
    monkeypatch.setattr(mc, "run_once", fake_crash)

    out = memory_consolidate_cron.tick(
        hours=1, multi_stage=False, now=1234.0,
    )
    # Tick still reports fired=True (we attempted) but error captured.
    assert out["fired"] is True
    assert "error" in out
    state = memory_consolidate_cron.read_state()
    # last_run_ts MUST be set despite the crash.
    assert state["last_run_ts"] == 1234.0
    assert "model endpoint down" in state["last_error"]


def test_tick_increments_runs_counter_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    import janus.memory_consolidate as mc
    monkeypatch.setattr(
        mc, "run_once",
        lambda *, max_input_cards=200: {"examined": 1, "written": 0},
    )
    # First fire
    memory_consolidate_cron.tick(hours=1, now=1000.0)
    # Second fire one full interval later
    memory_consolidate_cron.tick(hours=1, now=1000.0 + 3600)
    state = memory_consolidate_cron.read_state()
    assert state["runs"] == 2


def test_tick_calls_on_fire_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    import janus.memory_consolidate as mc
    monkeypatch.setattr(
        mc, "run_once",
        lambda *, max_input_cards=200: {"examined": 0, "written": 0},
    )
    fired_args: list[bool] = []
    memory_consolidate_cron.tick(
        hours=1, multi_stage=False, now=time.time(),
        on_fire=lambda ms: fired_args.append(ms),
    )
    assert fired_args == [False]


def test_tick_callback_exception_does_not_break(tmp_path, monkeypatch):
    """A buggy on_fire callback must not abort the tick."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    import janus.memory_consolidate as mc
    monkeypatch.setattr(
        mc, "run_once",
        lambda *, max_input_cards=200: {"examined": 0, "written": 0},
    )
    out = memory_consolidate_cron.tick(
        hours=1, multi_stage=False, now=time.time(),
        on_fire=lambda ms: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert out["fired"] is True


# ============================================================
# config exposure
# ============================================================


def test_config_constant_exists():
    assert hasattr(config, "MEMORY_CONSOLIDATE_HOURS")
    assert hasattr(config, "MEMORY_CONSOLIDATE_MULTI_STAGE")
    # Default is OFF
    assert isinstance(config.MEMORY_CONSOLIDATE_HOURS, int)


def test_env_var_picked_up(monkeypatch):
    """Re-import config picks up env. Smoke-test via fresh module load."""
    monkeypatch.setenv("JANUS_MEMORY_CONSOLIDATE_HOURS", "12")
    import importlib
    import janus.config as cfg_mod
    importlib.reload(cfg_mod)
    try:
        assert cfg_mod.MEMORY_CONSOLIDATE_HOURS == 12
    finally:
        monkeypatch.delenv("JANUS_MEMORY_CONSOLIDATE_HOURS", raising=False)
        importlib.reload(cfg_mod)


# ============================================================
# Daemon wire-up — source pin
# ============================================================


def test_daemon_calls_tick_in_loop():
    from janus.triggers import runtime as rt
    src = inspect.getsource(rt.run_daemon)
    assert "memory_consolidate_cron" in src
    assert "tick(" in src
    # Wrapped in try/except so a bug can't break the trigger daemon.
    assert "Exception" in src


def test_daemon_advertises_consolidate_in_startup():
    """User running `janus daemon` should see the cadence in the
    startup banner so they can confirm the env var registered."""
    from janus.triggers import runtime as rt
    src = inspect.getsource(rt.run_daemon)
    assert "consolidate" in src.lower()


# ============================================================
# memory_consolidate_cron module surface
# ============================================================


def test_module_exports():
    assert "tick" in memory_consolidate_cron.__all__
    assert "should_run_now" in memory_consolidate_cron.__all__
    assert "read_state" in memory_consolidate_cron.__all__
