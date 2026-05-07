"""Tests for v1.28.3 — multi-model fall-through (Phase 4).

When JANUS_MODEL_FALLBACK is set and the primary model fails with
an infra-shaped error (5xx after retries / ConnectionError /
Timeout), llm.chat() transparently tries the next model.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from janus import config, llm, model_fallback


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()
    return home


# ============================================================
# parse_chain
# ============================================================


def test_parse_chain_unset_returns_primary_only(monkeypatch):
    monkeypatch.delenv("JANUS_MODEL_FALLBACK", raising=False)
    chain = model_fallback.parse_chain("primary-model")
    assert chain == ["primary-model"]


def test_parse_chain_with_env_appends(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback_a,fallback_b")
    chain = model_fallback.parse_chain("primary")
    assert chain == ["primary", "fallback_a", "fallback_b"]


def test_parse_chain_strips_whitespace(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "  a  ,  b  ,c")
    chain = model_fallback.parse_chain("p")
    assert chain == ["p", "a", "b", "c"]


def test_parse_chain_dedupes_primary(monkeypatch):
    """Listing the primary in the env shouldn't double up."""
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "p,p,p")
    chain = model_fallback.parse_chain("p")
    assert chain == ["p"]


def test_parse_chain_dedupes_within_env(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "a,b,a,c,b")
    chain = model_fallback.parse_chain("p")
    assert chain == ["p", "a", "b", "c"]


def test_parse_chain_handles_empty_env(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "")
    assert model_fallback.parse_chain("primary") == ["primary"]


def test_parse_chain_skips_blank_tokens(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "a,,b,,,c")
    chain = model_fallback.parse_chain("p")
    assert chain == ["p", "a", "b", "c"]


# ============================================================
# is_fallback_trigger
# ============================================================


def test_trigger_connection_error():
    e = requests.exceptions.ConnectionError("refused")
    assert model_fallback.is_fallback_trigger(e) is True


def test_trigger_timeout():
    e = requests.exceptions.Timeout("slow")
    assert model_fallback.is_fallback_trigger(e) is True


def test_trigger_5xx_via_http_error():
    resp = MagicMock()
    resp.status_code = 503
    e = requests.HTTPError("server error", response=resp)
    assert model_fallback.is_fallback_trigger(e) is True


def test_no_trigger_4xx():
    """Client errors are NOT fallback triggers — switching model
    won't fix auth/context/unknown-model."""
    resp = MagicMock()
    resp.status_code = 401
    e = requests.HTTPError("auth", response=resp)
    assert model_fallback.is_fallback_trigger(e) is False


def test_no_trigger_404():
    resp = MagicMock()
    resp.status_code = 404
    e = requests.HTTPError("not found", response=resp)
    assert model_fallback.is_fallback_trigger(e) is False


def test_no_trigger_429():
    """429 is rate-limit; falling through to a different model just
    shifts the problem. _post_with_retry already backs off."""
    resp = MagicMock()
    resp.status_code = 429
    e = requests.HTTPError("rate limited", response=resp)
    assert model_fallback.is_fallback_trigger(e) is False


def test_no_trigger_random_exception():
    e = ValueError("not network")
    assert model_fallback.is_fallback_trigger(e) is False


# ============================================================
# reason_string
# ============================================================


def test_reason_connection():
    e = requests.exceptions.ConnectionError("refused")
    assert model_fallback.reason_string(e) == "connection_error"


def test_reason_timeout():
    e = requests.exceptions.Timeout("slow")
    assert model_fallback.reason_string(e) == "timeout"


def test_reason_5xx():
    resp = MagicMock()
    resp.status_code = 503
    e = requests.HTTPError("x", response=resp)
    assert model_fallback.reason_string(e) == "503"


def test_reason_unknown_class_name():
    e = ValueError("x")
    assert model_fallback.reason_string(e) == "ValueError"


# ============================================================
# record_fallback
# ============================================================


def test_record_fallback_writes_to_log(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    model_fallback.record_fallback(
        from_model="cheap", to_model="strong", reason="503",
    )
    log = (config.LOG_FILE).read_text(encoding="utf-8")
    assert "model_fallback" in log
    assert "cheap" in log
    assert "strong" in log
    assert "503" in log


def test_record_fallback_never_raises(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # Force a failure by making LOG_FILE unwritable
    monkeypatch.setattr(config, "LOG_FILE", Path("/nonexistent/log.jsonl"))
    # Must not raise
    model_fallback.record_fallback(
        from_model="a", to_model="b", reason="x",
    )


# ============================================================
# llm.chat fall-through integration
# ============================================================


def _make_5xx_response():
    resp = MagicMock()
    resp.status_code = 503
    resp.raise_for_status.side_effect = requests.HTTPError(
        "503 server error", response=resp,
    )
    resp.close = MagicMock()
    return resp


def _make_success_response(model: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [{
            "message": {"role": "assistant", "content": f"hello from {model}"},
        }],
        "usage": {
            "prompt_tokens": 10, "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
    return resp


def test_chat_falls_through_5xx_to_next_model(tmp_path, monkeypatch):
    """Primary model returns 503; fallback model returns 200 → we
    get the fallback's response."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback-model")

    posts = {"count": 0}

    def fake_post(url, **kwargs):
        posts["count"] += 1
        model = kwargs["json"]["model"]
        if model == "primary-model":
            return _make_5xx_response()
        return _make_success_response(model)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    # Cap retries low so 503 fail-through happens quickly
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)

    msg = llm.chat([{"role": "user", "content": "hi"}], model="primary-model")
    assert "fallback-model" in msg["content"]
    # At least one call to primary, then one to fallback
    assert posts["count"] >= 2


def test_chat_falls_through_connection_error(tmp_path, monkeypatch):
    """ConnectionError on primary → fall through to next."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback")

    def fake_post(url, **kwargs):
        if kwargs["json"]["model"] == "primary":
            raise requests.exceptions.ConnectionError("refused")
        return _make_success_response("fallback")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)

    msg = llm.chat([{"role": "user", "content": "hi"}], model="primary")
    assert "fallback" in msg["content"]


def test_chat_does_not_fall_through_4xx(tmp_path, monkeypatch):
    """401 / unknown model → don't switch. Re-raise."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback")

    posts = {"count": 0}

    def fake_post(url, **kwargs):
        posts["count"] += 1
        resp = MagicMock()
        resp.status_code = 401
        resp.raise_for_status.side_effect = requests.HTTPError(
            "401", response=resp,
        )
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)

    with pytest.raises(requests.HTTPError):
        llm.chat([{"role": "user", "content": "hi"}], model="primary")
    # Only the primary was tried (no fallback for 401)
    assert posts["count"] == 1


def test_chat_no_fallback_when_env_unset(tmp_path, monkeypatch):
    """Without JANUS_MODEL_FALLBACK, behavior is identical to
    pre-1.28.3 — primary fails, error propagates."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("JANUS_MODEL_FALLBACK", raising=False)

    def fake_post(url, **kwargs):
        return _make_5xx_response()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)

    with pytest.raises(requests.HTTPError):
        llm.chat([{"role": "user", "content": "hi"}], model="primary")


def test_chat_fall_through_records_event(tmp_path, monkeypatch):
    """After a successful fall-through, log.jsonl has a model_fallback
    record so audit replays show what happened."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "rescue")

    def fake_post(url, **kwargs):
        if kwargs["json"]["model"] == "primary":
            return _make_5xx_response()
        return _make_success_response("rescue")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)

    llm.chat([{"role": "user", "content": "hi"}], model="primary")
    log_text = config.LOG_FILE.read_text(encoding="utf-8")
    assert "model_fallback" in log_text
    assert "primary" in log_text
    assert "rescue" in log_text
    # Reason is the 503
    assert "503" in log_text


def test_chat_chain_exhausted_re_raises(tmp_path, monkeypatch):
    """All models in the chain fail → re-raise the last error."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "f1,f2")

    def fake_post(url, **kwargs):
        return _make_5xx_response()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)

    with pytest.raises(requests.HTTPError):
        llm.chat([{"role": "user", "content": "hi"}], model="primary")


# ============================================================
# EVENT_TYPES vocabulary pin
# ============================================================


def test_model_fallback_in_event_types():
    from janus.app import EVENT_TYPES
    assert "model_fallback" in EVENT_TYPES


# ============================================================
# llm source-pin: chat() routes through fall-through
# ============================================================


def test_chat_uses_model_fallback_module():
    import inspect
    src = inspect.getsource(llm.chat)
    assert "model_fallback" in src
    assert "parse_chain" in src
    assert "is_fallback_trigger" in src


def test_chat_attempt_helper_extracted():
    """The single-model attempt is extracted to _chat_attempt so
    chat() can wrap it in a loop."""
    import inspect
    assert hasattr(llm, "_chat_attempt")
    src = inspect.getsource(llm._chat_attempt)
    # Same shape as old chat() — POSTs once, returns the message dict
    assert "_post_with_retry" in src
    assert "raise_for_status" in src


def test_chat_propagates_keyboard_interrupt(tmp_path, monkeypatch):
    """KeyboardInterrupt must NOT be swallowed by the fall-through
    loop — Ctrl+C still works during a chat call."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback")

    def fake_post(url, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)

    with pytest.raises(KeyboardInterrupt):
        llm.chat([{"role": "user", "content": "hi"}], model="primary")
