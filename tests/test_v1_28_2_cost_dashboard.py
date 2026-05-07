"""Tests for v1.28.2 — cost dashboard + /cost command (Phase 4).

Adds:
  * ``cost.budget_status()`` — gauge based on JANUS_BUDGET_USD env var
  * ``cost.check_budget_alerts()`` — detect just-crossed thresholds
  * ``cost.render_budget_line()`` — one-line gauge for /cost
  * ``cost.daily_totals(since_days)`` + ``cost.render_daily()``
  * ``cost.reset_budget_alerts()`` — re-arm thresholds on /clear
  * ``budget_alert`` event in app.EVENT_TYPES
  * ``/cost --daily [N]`` subcommand
  * Post-turn alert hook in cli_rich
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from janus import config, cost


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()
    # Reset all cost state between tests
    cost.reset_session()
    cost.reset_budget_alerts()
    return home


# ============================================================
# budget_status — env var gauge
# ============================================================


def test_budget_status_returns_unconfigured_when_env_unset(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("JANUS_BUDGET_USD", raising=False)
    status = cost.budget_status()
    assert status["configured"] is False
    assert status["budget"] == 0.0


def test_budget_status_with_env_set(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "5.00")
    status = cost.budget_status()
    assert status["configured"] is True
    assert status["budget"] == 5.0
    assert status["spent"] == 0.0
    assert status["remaining"] == 5.0
    assert status["percent"] == 0.0


def test_budget_status_includes_session_spend(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 0.50  # half-spent
    status = cost.budget_status()
    assert status["spent"] == 0.50
    assert status["remaining"] == 0.50
    assert abs(status["percent"] - 0.5) < 1e-9


def test_budget_status_handles_over_budget(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 1.50
    status = cost.budget_status()
    assert status["percent"] > 1.0
    # remaining clamped at 0 (we don't show negative)
    assert status["remaining"] == 0.0


def test_budget_status_handles_invalid_env(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "not-a-number")
    status = cost.budget_status()
    assert status["configured"] is False


def test_budget_status_handles_zero_env(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "0")
    status = cost.budget_status()
    # Zero / unset are equivalent — no gauge to show
    assert status["configured"] is False


# ============================================================
# check_budget_alerts — threshold crossings
# ============================================================


def test_alerts_fire_on_just_crossed_50(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 0.55
    crossed = cost.check_budget_alerts()
    assert 0.5 in crossed
    assert 0.8 not in crossed
    assert 1.0 not in crossed


def test_alerts_fire_only_once_per_threshold(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 0.55
    first = cost.check_budget_alerts()
    second = cost.check_budget_alerts()
    assert 0.5 in first
    # Second call sees no NEW crossings (already alerted)
    assert second == []


def test_alerts_walk_thresholds_as_spend_grows(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 0.55
    cost.check_budget_alerts()  # fires 50%
    cost._SESSION.usd = 0.85
    crossed = cost.check_budget_alerts()
    # 50 already fired; 80 now fires; 100 not yet
    assert 0.8 in crossed
    assert 0.5 not in crossed
    assert 1.0 not in crossed
    # Cross 100
    cost._SESSION.usd = 1.10
    crossed = cost.check_budget_alerts()
    assert 1.0 in crossed


def test_alerts_no_fire_when_unconfigured(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("JANUS_BUDGET_USD", raising=False)
    cost._SESSION.usd = 9999.0
    assert cost.check_budget_alerts() == []


def test_alerts_no_fire_when_below_lowest_threshold(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 0.10  # 10% — below 50%
    assert cost.check_budget_alerts() == []


def test_reset_budget_alerts_re_arms(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 0.55
    first = cost.check_budget_alerts()
    assert 0.5 in first
    # /clear resets — same threshold should fire again
    cost.reset_budget_alerts()
    second = cost.check_budget_alerts()
    assert 0.5 in second


# ============================================================
# render_budget_line
# ============================================================


def test_render_budget_line_empty_when_unconfigured(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("JANUS_BUDGET_USD", raising=False)
    assert cost.render_budget_line() == ""


def test_render_budget_line_shows_gauge(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 0.50
    line = cost.render_budget_line()
    assert "$0.5" in line  # spent
    assert "1.00" in line  # budget
    assert "50.0%" in line


def test_render_budget_line_marks_over_budget(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_BUDGET_USD", "1.00")
    cost._SESSION.usd = 1.50
    line = cost.render_budget_line()
    assert "OVER BUDGET" in line


# ============================================================
# Daily totals from cost.jsonl
# ============================================================


def _write_ledger_row(home: Path, ts: str, **fields) -> None:
    row = {
        "ts": ts,
        "gateway": "cli_rich",
        "chat_id": "x",
        "identity": "",
        "model": "test-model",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "usd": 0.001,
    }
    row.update(fields)
    p = home / "cost.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_daily_totals_groups_by_date(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1)
    # 3 calls today, 1 yesterday
    _write_ledger_row(home, today.isoformat(), usd=0.10)
    _write_ledger_row(home, today.isoformat(), usd=0.20)
    _write_ledger_row(home, today.isoformat(), usd=0.30)
    _write_ledger_row(home, yesterday.isoformat(), usd=0.05)
    rolled = cost.daily_totals(since_days=7)
    assert len(rolled) == 2
    # Newest first
    assert rolled[0]["calls"] == 3
    assert abs(rolled[0]["usd"] - 0.60) < 1e-6
    assert rolled[1]["calls"] == 1


def test_daily_totals_filters_by_since_days(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)
    new = now - timedelta(days=2)
    _write_ledger_row(home, old.isoformat(), usd=99.0)
    _write_ledger_row(home, new.isoformat(), usd=0.10)
    rolled = cost.daily_totals(since_days=7)
    # Only new row falls within 7-day window
    assert len(rolled) == 1
    assert abs(rolled[0]["usd"] - 0.10) < 1e-6


def test_daily_totals_handles_missing_ledger(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert cost.daily_totals() == []


def test_daily_totals_skips_malformed_rows(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    p = home / "cost.jsonl"
    today = datetime.now(timezone.utc).isoformat()
    p.write_text(
        json.dumps({"ts": today, "usd": 0.10}) + "\n"
        + "not json at all\n"
        + json.dumps({"no_ts_field": True}) + "\n"
        + json.dumps({"ts": "not-iso", "usd": 0.10}) + "\n"
        + json.dumps({"ts": today, "usd": 0.20}) + "\n",
        encoding="utf-8",
    )
    rolled = cost.daily_totals()
    # Only the 2 valid rows count
    assert len(rolled) == 1
    assert abs(rolled[0]["usd"] - 0.30) < 1e-6


def test_render_daily_returns_text(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    today = datetime.now(timezone.utc)
    _write_ledger_row(home, today.isoformat(), usd=0.50)
    out = cost.render_daily(since_days=7)
    assert "daily" in out.lower()
    assert today.date().isoformat() in out


def test_render_daily_when_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    out = cost.render_daily()
    assert "no" in out.lower()


# ============================================================
# EVENT_TYPES vocabulary pin
# ============================================================


def test_budget_alert_in_event_types():
    from janus.app import EVENT_TYPES
    assert "budget_alert" in EVENT_TYPES


# ============================================================
# cli_rich source-pins
# ============================================================


def test_cli_rich_cost_handler_supports_daily_subcommand():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "--daily" in src
    assert "render_daily" in src


def test_cli_rich_cost_handler_renders_budget_line():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "render_budget_line" in src


def test_cli_rich_clear_resets_budget_alerts():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "reset_budget_alerts" in src


def test_cli_rich_post_turn_runs_check_budget_alerts():
    """Source-pin: the after-turn flow calls check_budget_alerts so
    the user sees the warning inline rather than only via /cost."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "check_budget_alerts" in src
    # And the call is inside a try/except so it can't crash the loop
    idx = src.find("check_budget_alerts")
    pre = src[max(0, idx - 200):idx]
    assert "try:" in pre


def test_cli_rich_post_turn_alert_uses_v1_28_2_marker():
    """Stable comment marker so future test changes can find the
    block independently of file-level rearrangement."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "v1.28.2: budget alert" in src
