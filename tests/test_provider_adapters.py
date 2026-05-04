"""tests/test_provider_adapters.py — v1.14.0 (Tier B closer)."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest

from janus import config, llm, rate_limit


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()
    rate_limit.reset()


# ============================================================
# 529 (Anthropic overloaded) is retryable
# ============================================================


def test_529_added_to_retryable_status():
    assert 529 in llm._RETRYABLE_STATUS


def test_post_with_retry_retries_on_529(monkeypatch):
    monkeypatch.setattr(llm, "_backoff_sleep", lambda *a, **kw: None)
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 3)

    responses = [
        MagicMock(status_code=529),
        MagicMock(status_code=529),
        MagicMock(status_code=200),
    ]

    def fake_post(url, **kw):
        return responses.pop(0)

    monkeypatch.setattr("requests.post", fake_post)

    r = llm._post_with_retry(
        "https://x", headers={}, json_payload={"model": "m"}, timeout=5,
    )
    assert r.status_code == 200


# ============================================================
# Provider-aware backoff
# ============================================================


def test_backoff_sleep_uses_provider_cooldown_floor(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(config, "LLM_RETRY_BACKOFF_BASE_S", 1.0)

    # base * 2^0 = ~1s + jitter. provider says 30s. 30s should win.
    llm._backoff_sleep(0, provider_cooldown=30.0)
    assert sleeps[-1] >= 30.0


def test_backoff_sleep_no_cooldown_uses_normal_jitter(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(config, "LLM_RETRY_BACKOFF_BASE_S", 1.0)
    llm._backoff_sleep(0, provider_cooldown=0.0)
    assert sleeps[-1] < 5.0


def test_post_with_retry_sleeps_before_first_attempt_when_cooldown(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_BASE", "https://api.anthropic.com/v1")

    # Pre-arm the rate-limit tracker with a recent 429.
    rate_limit.record_request(
        provider="anthropic", model="claude-test",
        ok=False, status_429=True,
    )

    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("requests.post", lambda url, **kw: MagicMock(status_code=200))

    llm._post_with_retry(
        "https://x", headers={}, json_payload={"model": "claude-test"}, timeout=5,
    )

    # Should have slept BEFORE the first request — provider was in cooldown.
    assert sleeps  # non-empty
    assert sleeps[0] > 0.0


# ============================================================
# Cache markers (v1.14 — also marks last user msg if substantial)
# ============================================================


def test_cache_markers_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", False)
    msgs = [
        {"role": "system", "content": "x" * 2000},
        {"role": "user", "content": "y" * 2000},
    ]
    assert llm.apply_cache_markers(msgs) == msgs


def test_cache_markers_marks_last_system(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "u"},
    ]
    out = llm.apply_cache_markers(msgs)
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_markers_marks_last_user_when_substantial(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "x" * 2000},  # ≥1024
    ]
    out = llm.apply_cache_markers(msgs)
    assert isinstance(out[1]["content"], list)
    assert out[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_markers_skips_short_user_message(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "short"},
    ]
    out = llm.apply_cache_markers(msgs)
    # System marked, user untouched (still a string)
    assert isinstance(out[0]["content"], list)
    assert isinstance(out[1]["content"], str)


def test_cache_markers_skips_already_listed_content(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    msgs = [
        {"role": "system", "content": [{"type": "text", "text": "pre-built"}]},
        {"role": "user", "content": "u"},
    ]
    out = llm.apply_cache_markers(msgs)
    # Caller already built blocks — preserved untouched
    assert out[0]["content"] == [{"type": "text", "text": "pre-built"}]


# ============================================================
# Provider name detection
# ============================================================


def test_provider_from_openrouter():
    assert llm._provider_from_base("https://openrouter.ai/api/v1") == "openrouter"


def test_provider_from_anthropic():
    assert llm._provider_from_base("https://api.anthropic.com/v1") == "anthropic"


def test_provider_from_openai():
    assert llm._provider_from_base("https://api.openai.com/v1") == "openai"


def test_provider_from_localhost():
    assert llm._provider_from_base("http://localhost:11434/v1") == "local"
    assert llm._provider_from_base("http://127.0.0.1:8080/v1") == "local"


def test_provider_from_unknown_returns_host():
    out = llm._provider_from_base("https://my-custom-llm.example.com/v1")
    assert out == "my-custom-llm.example.com"


def test_provider_from_garbage_returns_unknown():
    # When urlparse can't extract a netloc, fall back to "unknown".
    out = llm._provider_from_base("")
    assert out == "unknown"
