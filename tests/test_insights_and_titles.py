"""tests/test_insights_and_titles.py — v1.9.0 Tier A item 3."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from janus import config, insights, title_generator
from janus.conversation import Conversation


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(config, "TRIGGERS_DIR", home / "triggers")
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "USER_MODEL_FILE", home / "user.md")
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "DAEMON_STATE", home / "daemon.state.json")
    monkeypatch.setattr(config, "EVALS_DIR", home / "evals")
    monkeypatch.setattr(config, "MCP_DIR", home / "mcp")
    monkeypatch.setattr(config, "CONVERSATIONS_DIR", home / "conversations")
    monkeypatch.setattr(config, "COMMANDS_DIR", home / "commands")
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", home / "swarms" / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", home / "swarms" / "runs")
    config.ensure_home()


# ---------- Insights ----------


def test_compute_insights_empty_install(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    stats = insights.compute_insights(days=7)
    assert stats["window_days"] == 7
    assert stats["activity"]["turn_count"] == 0
    assert stats["cost"]["total_usd"] == 0
    assert stats["agents"]["fire_count"] == 0
    assert stats["sessions"]["count"] == 0


def test_compute_insights_clamps_days_to_min_1(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    stats = insights.compute_insights(days=0)
    assert stats["window_days"] == 1


def test_activity_stats_counts_in_window(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    now = dt.datetime.now(dt.timezone.utc)
    in_window = now - dt.timedelta(days=2)
    out_of_window = now - dt.timedelta(days=30)
    rows = [
        {"ts": in_window.isoformat(), "type": "turn", "tool": "fs_read"},
        {"ts": in_window.isoformat(), "type": "turn", "tool": "fs_grep"},
        {"ts": in_window.isoformat(), "type": "turn", "tool": "fs_read"},
        {"ts": out_of_window.isoformat(), "type": "turn", "tool": "shell"},
    ]
    config.LOG_FILE.write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8",
    )
    stats = insights.compute_insights(days=7)
    a = stats["activity"]
    assert a["turn_count"] == 3
    assert a["tool_call_count"] == 3
    assert a["by_tool"]["fs_read"] == 2
    assert "shell" not in a["by_tool"]


def test_cost_stats_sums_window(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    now = dt.datetime.now(dt.timezone.utc)
    rows = [
        {"ts": now.isoformat(), "model": "gpt-oss:120b", "cost_usd": 0.10},
        {"ts": now.isoformat(), "model": "gpt-oss:120b", "cost_usd": 0.05},
        {"ts": (now - dt.timedelta(days=30)).isoformat(),
         "model": "gpt-oss:120b", "cost_usd": 1.00},
    ]
    cost_file = config.HOME / "cost.jsonl"
    cost_file.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    stats = insights.compute_insights(days=7)
    c = stats["cost"]
    assert c["total_usd"] == 0.15
    assert c["by_model"]["gpt-oss:120b"] == 0.15


def test_agent_stats_counts_recent_fires(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    archive = config.HOME / "cron" / "output" / "newsbot"
    archive.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-00-00+00-00")
    long_ago = "2020-01-01T00-00-00+00-00"
    (archive / f"{today}.md").write_text("---\nx\n---\n\nbody\n", encoding="utf-8")
    (archive / f"{long_ago}.md").write_text("---\nx\n---\n\nbody\n", encoding="utf-8")
    stats = insights.compute_insights(days=7)
    a = stats["agents"]
    assert a["fire_count"] == 1
    assert a["by_agent"]["newsbot"] == 1


def test_memory_stats_lists_category_sizes(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (config.MEMORY_DIR / "user.md").write_text("# user\n\nlots of content\n",
                                                encoding="utf-8")
    (config.MEMORY_DIR / "project.md").write_text("# project\n\nshort\n",
                                                   encoding="utf-8")
    stats = insights.compute_insights(days=7)
    m = stats["memory"]
    assert "user" in m["category_sizes"]
    assert "project" in m["category_sizes"]
    assert m["category_sizes"]["user"] > m["category_sizes"]["project"]


def test_session_stats_counts_recent_conversations(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    config.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    (config.CONVERSATIONS_DIR / "x.json").write_text(json.dumps({
        "id": "x",
        "started": now.isoformat(),
        "last_updated": now.isoformat(),
        "model": "gpt-oss:120b",
        "title": "Refactor memory module",
        "turns": [],
    }), encoding="utf-8")
    (config.CONVERSATIONS_DIR / "old.json").write_text(json.dumps({
        "id": "old",
        "last_updated": (now - dt.timedelta(days=30)).isoformat(),
        "title": "Old conv",
        "turns": [],
    }), encoding="utf-8")
    stats = insights.compute_insights(days=7)
    s = stats["sessions"]
    assert s["count"] == 1
    assert s["recent_titles"][0]["title"] == "Refactor memory module"


def test_render_insights_includes_all_sections(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = insights.render_insights(insights.compute_insights(days=7))
    for section in ("Insights", "Activity", "Cost", "Scheduled agents",
                    "Memory", "Sessions"):
        assert section in out


# ---------- Title generator ----------


def _make_conv(first_request: str = "Refactor the memory module please.") -> Conversation:
    return Conversation(
        id="t",
        started="2026-05-04T00:00:00+00:00",
        last_updated="2026-05-04T00:00:00+00:00",
        model="gpt-oss:120b",
        workspace="/tmp",
        turns=[{
            "ts": "2026-05-04T00:00:00+00:00",
            "request": first_request,
            "output": "Sure, I'll start by reading memory.py.",
        }],
    )


def test_should_generate_title_skips_when_already_set():
    c = _make_conv()
    c.title = "already named"
    assert title_generator.should_generate_title(c) is False


def test_should_generate_title_skips_short_input():
    c = _make_conv(first_request="hi")
    assert title_generator.should_generate_title(c) is False


def test_should_generate_title_skips_greeting():
    c = _make_conv(first_request="Hello there friend, how are you?")
    assert title_generator.should_generate_title(c) is False


def test_should_generate_title_skips_when_env_disabled(monkeypatch):
    monkeypatch.setenv("JANUS_TITLE_AUTO", "0")
    c = _make_conv()
    assert title_generator.should_generate_title(c) is False


def test_should_generate_title_accepts_substantive_input():
    c = _make_conv()
    assert title_generator.should_generate_title(c) is True


def test_normalize_title_strips_quotes_and_punctuation():
    assert title_generator._normalize_title('"Refactor memory module."') == "Refactor memory module"
    assert title_generator._normalize_title("'wrap output'") == "Wrap output"
    assert title_generator._normalize_title("compare janus and hermes...") == "Compare janus and hermes"


def test_normalize_title_caps_length():
    long = "x " * 100
    out = title_generator._normalize_title(long)
    assert len(out) <= 80
    assert out.endswith("…")


def test_maybe_generate_returns_false_on_llm_failure(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    c = _make_conv()
    # Force LLM call to fail.
    monkeypatch.setattr("janus.title_generator.generate_title", lambda x: "")
    ok = title_generator.maybe_generate(c)
    assert ok is False
    assert c.title == ""


def test_maybe_generate_assigns_title_on_success(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    c = _make_conv()
    monkeypatch.setattr(
        "janus.title_generator.generate_title",
        lambda x: "Refactor memory module",
    )
    ok = title_generator.maybe_generate(c)
    assert ok is True
    assert c.title == "Refactor memory module"


def test_conversation_round_trip_preserves_title(tmp_path, monkeypatch):
    """Title field must round-trip through save → load (was missing
    before v1.9 added it to load())."""
    _isolate_home(tmp_path, monkeypatch)
    from janus import conversation as cm
    c = _make_conv()
    c.title = "Hermes parity work"
    cm.save(c)
    loaded = cm.load(c.id)
    assert loaded is not None
    assert loaded.title == "Hermes parity work"
