"""tests/test_trajectory_and_onboarding.py — v1.13.0 Tier B mid."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from janus import config, trajectory, onboarding


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
    # Reset thread-local writer between tests.
    if hasattr(trajectory._LOCAL, "writer"):
        delattr(trajectory._LOCAL, "writer")


# ============================================================
# Trajectory recording
# ============================================================


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv("JANUS_TRAJECTORY", raising=False)
    assert trajectory.is_enabled() is False


def test_is_enabled_when_set(monkeypatch):
    monkeypatch.setenv("JANUS_TRAJECTORY", "1")
    assert trajectory.is_enabled() is True
    monkeypatch.setenv("JANUS_TRAJECTORY", "true")
    assert trajectory.is_enabled() is True
    monkeypatch.setenv("JANUS_TRAJECTORY", "off")
    assert trajectory.is_enabled() is False


def test_open_trajectory_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("JANUS_TRAJECTORY", raising=False)
    assert trajectory.open_trajectory("conv1") is None


def test_writer_writes_events(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_TRAJECTORY", "1")
    monkeypatch.setenv("JANUS_REDACT", "off")  # prevent secret-pattern false positives in test
    w = trajectory.open_trajectory("convA")
    assert w is not None
    with w:
        trajectory.record({"type": "system", "content": "system text"})
        trajectory.record({"type": "user", "content": "hello"})
        trajectory.record({"type": "assistant_final", "content": "hi"})
    events = trajectory.read_trajectory(w.path)
    assert len(events) == 3
    assert events[0]["type"] == "system"
    assert events[1]["content"] == "hello"
    assert events[2]["content"] == "hi"
    # Auto-stamped ts on every event
    assert all("ts" in e for e in events)


def test_record_noop_when_no_writer(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    # No writer active — record should silently do nothing
    trajectory.record({"type": "x"})
    # If we got here without an exception, that's the behavior.


def test_writer_redacts_secrets(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_TRAJECTORY", "1")
    monkeypatch.setenv("JANUS_REDACT", "conservative")
    w = trajectory.open_trajectory("convB")
    with w:
        trajectory.record({
            "type": "user",
            "content": "key sk-1234567890abcdefghij12345 in here",
        })
    text = w.path.read_text(encoding="utf-8")
    assert "<REDACTED:openai_key>" in text
    assert "sk-1234567890" not in text


def test_list_trajectories(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_TRAJECTORY", "1")
    monkeypatch.setenv("JANUS_REDACT", "off")
    w1 = trajectory.open_trajectory("convA")
    with w1:
        trajectory.record({"type": "x"})
    w2 = trajectory.open_trajectory("convB")
    with w2:
        trajectory.record({"type": "y"})
    items = trajectory.list_trajectories()
    assert len(items) == 2
    convs = {it["conv_id"] for it in items}
    assert convs == {"convA", "convB"}


def test_list_trajectories_filter_by_conv_id(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_TRAJECTORY", "1")
    monkeypatch.setenv("JANUS_REDACT", "off")
    with trajectory.open_trajectory("a") as w:
        trajectory.record({"type": "x"})
    with trajectory.open_trajectory("b") as w:
        trajectory.record({"type": "y"})
    items = trajectory.list_trajectories(conv_id="a")
    assert len(items) == 1
    assert items[0]["conv_id"] == "a"


def test_read_trajectory_skips_malformed_lines(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = tmp_path / "broken.jsonl"
    p.write_text(
        '{"type": "good"}\n'
        'not json at all\n'
        '{"type": "also good"}\n',
        encoding="utf-8",
    )
    events = trajectory.read_trajectory(p)
    assert len(events) == 2
    assert events[0]["type"] == "good"
    assert events[1]["type"] == "also good"


def test_read_trajectory_returns_empty_for_missing_file():
    assert trajectory.read_trajectory("/nonexistent.jsonl") == []


def test_writer_failure_silent_on_disk_error(tmp_path, monkeypatch):
    """If the trajectory file can't be opened, recording must NOT crash."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_TRAJECTORY", "1")
    w = trajectory.open_trajectory("conv")
    # Force the open to fail
    w._fh = None
    w.write_event({"type": "x"})  # should not raise


def test_safe_filename_for_weird_conv_id(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_TRAJECTORY", "1")
    monkeypatch.setenv("JANUS_REDACT", "off")
    w = trajectory.open_trajectory("conv with /weird\\chars")
    with w:
        trajectory.record({"type": "x"})
    # Path should be sanitized — no slashes / backslashes in the dir name
    parent_name = w.path.parent.name
    assert "/" not in parent_name
    assert "\\" not in parent_name


# ============================================================
# Onboarding wizard
# ============================================================


class _StubPrompt:
    """Replays scripted answers to simulate user input."""
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, prompt_text: str = "") -> str:
        self.calls.append(prompt_text)
        if not self.answers:
            return ""
        return self.answers.pop(0)


def test_run_wizard_completes_with_minimum_inputs(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    answers = [
        "1",        # provider: OpenRouter
        "sk-or-test-key-12345",  # api key
        "1",        # model: first popular
        "",         # mode: skip
        "n",        # skill install: no
        "n",        # telegram: no
    ]
    out: list[str] = []
    ok = onboarding.run_wizard(prompt=_StubPrompt(answers), output=out.append)
    assert ok is True
    env_path = config.HOME / ".env"
    text = env_path.read_text(encoding="utf-8")
    assert "JANUS_API_BASE=https://openrouter.ai/api/v1" in text
    assert "JANUS_API_KEY=sk-or-test-key-12345" in text
    assert "JANUS_MODEL=" in text


def test_run_wizard_aborts_on_no_provider(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out: list[str] = []
    ok = onboarding.run_wizard(prompt=_StubPrompt([""]), output=out.append)
    assert ok is False
    assert any("aborting" in line for line in out)


def test_run_wizard_aborts_on_invalid_provider(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out: list[str] = []
    ok = onboarding.run_wizard(prompt=_StubPrompt(["999"]), output=out.append)
    assert ok is False


def test_run_wizard_aborts_on_no_api_key(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out: list[str] = []
    ok = onboarding.run_wizard(
        prompt=_StubPrompt(["1", ""]), output=out.append,
    )
    assert ok is False


def test_run_wizard_writes_telegram_when_chosen(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    answers = [
        "1",        # OpenRouter
        "key",      # api key
        "custom-model",  # custom model
        "",         # mode skip
        "n",        # no skills
        "y",        # YES telegram
        "1234567890:ABC",  # token
        "111,222",  # chats
    ]
    out: list[str] = []
    ok = onboarding.run_wizard(prompt=_StubPrompt(answers), output=out.append)
    assert ok is True
    env_text = (config.HOME / ".env").read_text(encoding="utf-8")
    assert "JANUS_TELEGRAM_TOKEN=" in env_text
    assert "1234567890:ABC" in env_text
    assert "JANUS_TELEGRAM_CHATS=" in env_text


def test_run_wizard_sets_permission_mode(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    answers = [
        "1", "key", "custom",  # provider/key/model
        "4",   # mode: auto
        "n", "n",
    ]
    onboarding.run_wizard(prompt=_StubPrompt(answers), output=lambda x: None)
    env_text = (config.HOME / ".env").read_text(encoding="utf-8")
    assert "JANUS_APPROVAL=auto" in env_text


def test_upsert_env_preserves_unrelated_lines(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    env = config.HOME / ".env"
    env.write_text(
        "# my comment\n"
        "OTHER_VAR=preserved\n"
        "JANUS_MODEL=old-model\n",
        encoding="utf-8",
    )
    onboarding._upsert_env(env, {"JANUS_MODEL": "new-model"})
    text = env.read_text(encoding="utf-8")
    assert "# my comment" in text
    assert "OTHER_VAR=preserved" in text
    assert "JANUS_MODEL=new-model" in text
    assert "old-model" not in text


def test_upsert_env_appends_new_keys(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    env = config.HOME / ".env"
    env.write_text("EXISTING=x\n", encoding="utf-8")
    onboarding._upsert_env(env, {"JANUS_API_KEY": "sk-new"})
    text = env.read_text(encoding="utf-8")
    assert "EXISTING=x" in text
    assert "JANUS_API_KEY=sk-new" in text


def test_dotenv_quote_handles_spaces():
    out = onboarding._dotenv_quote("hello world")
    assert out.startswith('"') and out.endswith('"')
    assert "hello world" in out


def test_dotenv_quote_handles_quotes():
    out = onboarding._dotenv_quote('val with "quotes"')
    # Inner quotes escaped
    assert '\\"' in out


def test_dotenv_quote_simple_value_unquoted():
    assert onboarding._dotenv_quote("simple") == "simple"


def test_dotenv_quote_empty_returns_empty_quoted():
    assert onboarding._dotenv_quote("") == '""'
