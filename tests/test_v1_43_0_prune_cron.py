"""Tests for v1.43.0 — prune_cron daemon ticks.

The two ticks (memory + skill) share state and shape with the v1.30.2
memory_consolidate_cron, but they're pure-compute (no LLM cost) so the
defaults flip: on at 24h, opt-out via env var. These tests pin:

  * disabled when hours <= 0
  * first-run fires immediately when enabled
  * within-interval = not_due
  * at-interval / past-interval = fires
  * state round-trip + corrupt state handled
  * tick marks last_run BEFORE delegating (so a hang doesn't loop)
  * tick swallows downstream exceptions and records last_error
"""

from __future__ import annotations

import time
from pathlib import Path

from janus import config, prune_cron


# ============================================================
# should_run_now
# ============================================================


def test_disabled_returns_false():
    assert prune_cron.should_run_now({}, key="any", hours=0) is False


def test_disabled_even_with_state():
    state = {"last_memory_prune_ts": 1.0}
    assert prune_cron.should_run_now(
        state, key="last_memory_prune_ts", hours=0, now=10000.0,
    ) is False


def test_first_run_fires_immediately():
    assert prune_cron.should_run_now(
        {}, key="last_memory_prune_ts", hours=24, now=time.time(),
    ) is True


def test_within_interval_not_due():
    state = {"last_memory_prune_ts": 1000.0}
    assert prune_cron.should_run_now(
        state, key="last_memory_prune_ts", hours=1, now=1030.0,
    ) is False


def test_at_interval_fires():
    state = {"last_memory_prune_ts": 1000.0}
    assert prune_cron.should_run_now(
        state, key="last_memory_prune_ts", hours=1, now=1000.0 + 3600,
    ) is True


def test_corrupt_last_run_treated_as_first_run():
    state = {"last_memory_prune_ts": "yesterday"}
    assert prune_cron.should_run_now(
        state, key="last_memory_prune_ts", hours=24, now=time.time(),
    ) is True


# ============================================================
# state round-trip
# ============================================================


def test_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    state = {"last_memory_prune_ts": 1234.5, "memory_prune_runs": 2}
    prune_cron.write_state(state)
    assert prune_cron.read_state() == state


def test_read_state_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert prune_cron.read_state() == {}


def test_read_state_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    prune_cron.state_path().parent.mkdir(parents=True, exist_ok=True)
    prune_cron.state_path().write_text("{not json", encoding="utf-8")
    assert prune_cron.read_state() == {}


# ============================================================
# tick_memory
# ============================================================


def test_tick_memory_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    out = prune_cron.tick_memory(hours=0, now=time.time())
    assert out == {"fired": False, "reason": "disabled"}


def test_tick_memory_fires_first_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)

    fake = {"removed": 3, "active_drops": 1, "low_conf_drops": 2, "superseded_drops": 0}

    import janus.memory_prune as mp
    monkeypatch.setattr(mp, "run_once", lambda **kw: fake)

    out = prune_cron.tick_memory(hours=24, now=time.time())
    assert out["fired"] is True
    assert out["removed"] == 3
    # state was written
    state = prune_cron.read_state()
    assert "last_memory_prune_ts" in state
    assert state["memory_prune_runs"] == 1


def test_tick_memory_not_due(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    # Seed state as if we ran 30s ago at 24h cadence.
    now = time.time()
    prune_cron.write_state({"last_memory_prune_ts": now - 30})
    out = prune_cron.tick_memory(hours=24, now=now)
    assert out == {"fired": False, "reason": "not_due"}


def test_tick_memory_marks_last_run_before_failure(tmp_path, monkeypatch):
    """Hang/crash invariant: state is marked BEFORE delegating so the
    next tick doesn't replay immediately."""
    monkeypatch.setattr(config, "HOME", tmp_path)

    def explode(**kw):
        raise RuntimeError("simulated prune failure")

    import janus.memory_prune as mp
    monkeypatch.setattr(mp, "run_once", explode)

    now = time.time()
    out = prune_cron.tick_memory(hours=24, now=now)
    assert out["fired"] is True
    assert "error" in out
    state = prune_cron.read_state()
    assert state["last_memory_prune_ts"] == now
    assert "simulated prune failure" in state["last_memory_prune_error"]


# ============================================================
# tick_skill
# ============================================================


def test_tick_skill_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    out = prune_cron.tick_skill(hours=0, now=time.time())
    assert out == {"fired": False, "reason": "disabled"}


def test_tick_skill_fires_first_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)

    fake = {"removed": 1, "trashed": 1, "stale_marked": 0, "unlinked": 0}

    import janus.skill_prune as sp
    monkeypatch.setattr(sp, "run_once", lambda **kw: fake)

    out = prune_cron.tick_skill(hours=24, now=time.time())
    assert out["fired"] is True
    assert out["trashed"] == 1
    state = prune_cron.read_state()
    assert "last_skill_prune_ts" in state
    assert state["skill_prune_runs"] == 1


def test_memory_and_skill_independent(tmp_path, monkeypatch):
    """Firing memory tick must not satisfy the skill tick's interval."""
    monkeypatch.setattr(config, "HOME", tmp_path)

    import janus.memory_prune as mp
    import janus.skill_prune as sp
    monkeypatch.setattr(mp, "run_once", lambda **kw: {"removed": 0})
    monkeypatch.setattr(sp, "run_once", lambda **kw: {"removed": 0})

    now = time.time()
    prune_cron.tick_memory(hours=24, now=now)
    # State has last_memory_prune_ts but NOT last_skill_prune_ts
    state = prune_cron.read_state()
    assert "last_memory_prune_ts" in state
    assert "last_skill_prune_ts" not in state

    # Skill tick still fires on its own first-run path
    out = prune_cron.tick_skill(hours=24, now=now)
    assert out["fired"] is True
