"""Tests for v1.29.2 — streaming model fall-through.

v1.28.3 added fall-through to llm.chat() but explicitly punted on
streaming. v1.29.2 closes that gap with the SAFE policy:

  * BEFORE first chunk: 5xx / Connection / Timeout → fall through.
  * AFTER any chunk: re-raise. Switching models mid-stream would
    chimera the output (A prefix + B suffix).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from janus import config, streaming


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "API_BASE", "https://example/v1")
    monkeypatch.setattr(config, "LLM_RETRY_MAX_ATTEMPTS", 1)
    config.ensure_home()
    return home


def _make_streaming_response_lines(model_label: str) -> list[str]:
    """SSE-formatted lines that the parser will consume into deltas."""
    import json as _json
    chunks = []
    chunks.append("data: " + _json.dumps({
        "choices": [{
            "delta": {"content": f"hello-from-{model_label}"},
            "index": 0,
        }],
    }))
    chunks.append("data: " + _json.dumps({
        "choices": [{"delta": {}, "index": 0,
                     "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1, "completion_tokens": 2,
            "total_tokens": 3,
        },
    }))
    chunks.append("data: [DONE]")
    return chunks


def _make_success_response(model_label: str):
    """A live MagicMock that mimics requests.Response with iter_lines."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = iter(
        _make_streaming_response_lines(model_label)
    )
    resp.close = MagicMock()
    # __enter__/__exit__ for context-manager use in _post_with_retry
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_5xx_response():
    resp = MagicMock()
    resp.status_code = 503
    resp.raise_for_status.side_effect = requests.HTTPError(
        "503 server error", response=resp,
    )
    resp.close = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ============================================================
# Single-attempt _stream_one extracted (source-pin)
# ============================================================


def test_stream_one_extracted():
    """v1.29.2 extraction: _stream_one is a single-model attempt
    callable that the public chat_stream wraps in a fall-through
    loop."""
    assert hasattr(streaming, "_stream_one")
    import inspect
    src = inspect.getsource(streaming._stream_one)
    # Same shape as old chat_stream — POST + raise_for_status +
    # iter_lines + assemble.
    assert "_post_with_retry" in src
    assert "iter_lines" in src
    assert "yield text" in src or "yield" in src


def test_chat_stream_uses_model_fallback_module():
    import inspect
    src = inspect.getsource(streaming.chat_stream)
    assert "model_fallback" in src
    assert "parse_chain" in src
    assert "is_fallback_trigger" in src


def test_chat_stream_tracks_any_yielded_for_safety():
    """Source-pin: chat_stream tracks whether any chunk has been
    yielded, and if so, mid-stream failures re-raise instead of
    falling through (no chimera output)."""
    import inspect
    src = inspect.getsource(streaming.chat_stream)
    assert "any_yielded" in src
    # Policy: if any_yielded and fallback trigger → still raise
    assert "any_yielded" in src
    # Comment / code path explains why
    assert "chimera" in src.lower() or "switching" in src.lower()


# ============================================================
# Fall-through: 5xx before first chunk → next model
# ============================================================


def test_streaming_falls_through_5xx_before_first_chunk(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback-model")

    posts = {"count": 0}

    def fake_post(url, **kwargs):
        posts["count"] += 1
        model = kwargs["json"]["model"]
        if model == "primary-model":
            return _make_5xx_response()
        return _make_success_response("fallback")

    monkeypatch.setattr(requests, "post", fake_post)

    chunks = list(streaming.chat_stream(
        [{"role": "user", "content": "hi"}], model="primary-model",
    ))
    # First yields are text deltas; final yield is the assembled dict.
    text_chunks = [c for c in chunks if isinstance(c, str)]
    final = next(c for c in chunks if isinstance(c, dict))
    assert any("fallback" in t for t in text_chunks)
    assert "fallback" in final["content"]
    # Both models attempted: primary failed 5xx, fallback succeeded.
    assert posts["count"] >= 2


def test_streaming_falls_through_connection_error(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "rescue")

    def fake_post(url, **kwargs):
        if kwargs["json"]["model"] == "primary":
            raise requests.exceptions.ConnectionError("refused")
        return _make_success_response("rescue")

    monkeypatch.setattr(requests, "post", fake_post)

    chunks = list(streaming.chat_stream(
        [{"role": "user", "content": "hi"}], model="primary",
    ))
    text_joined = "".join(c for c in chunks if isinstance(c, str))
    assert "rescue" in text_joined


def test_streaming_no_fall_through_on_4xx(tmp_path, monkeypatch):
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
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(requests.HTTPError):
        list(streaming.chat_stream(
            [{"role": "user", "content": "hi"}], model="primary",
        ))
    # Only the primary was tried.
    assert posts["count"] == 1


def test_streaming_no_fallback_when_env_unset(tmp_path, monkeypatch):
    """Without JANUS_MODEL_FALLBACK, behavior is identical to
    pre-1.29.2 — primary fails, error propagates."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("JANUS_MODEL_FALLBACK", raising=False)

    monkeypatch.setattr(requests, "post", lambda url, **kw: _make_5xx_response())

    with pytest.raises(requests.HTTPError):
        list(streaming.chat_stream(
            [{"role": "user", "content": "hi"}], model="primary",
        ))


def test_streaming_chain_exhausted_re_raises(tmp_path, monkeypatch):
    """All models in the chain fail before first chunk → re-raise
    the last error."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "f1,f2")
    monkeypatch.setattr(requests, "post", lambda url, **kw: _make_5xx_response())

    with pytest.raises(requests.HTTPError):
        list(streaming.chat_stream(
            [{"role": "user", "content": "hi"}], model="primary",
        ))


# ============================================================
# Mid-stream failure: re-raise (no chimera)
# ============================================================


def test_streaming_mid_stream_failure_re_raises(tmp_path, monkeypatch):
    """If iter_lines yields some text then raises, do NOT fall
    through — the consumer has already received text from the
    primary; switching would chimera the output."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback")

    def crashing_iter():
        # First yield a chunk, then crash with a "fall-back trigger"
        # exception that would otherwise route to the next model.
        import json as _json
        yield "data: " + _json.dumps({
            "choices": [{"delta": {"content": "first-half"}, "index": 0}],
        })
        raise requests.exceptions.ConnectionError("mid-stream drop")

    def fake_post(url, **kwargs):
        if kwargs["json"]["model"] == "primary":
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            resp.iter_lines.return_value = crashing_iter()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp
        return _make_success_response("fallback")

    monkeypatch.setattr(requests, "post", fake_post)

    # Consumer should see "first-half" text, THEN a ConnectionError.
    # No "fallback" content — switching mid-stream is forbidden.
    chunks: list = []
    with pytest.raises(requests.exceptions.ConnectionError):
        for c in streaming.chat_stream(
            [{"role": "user", "content": "hi"}], model="primary",
        ):
            chunks.append(c)
    text_chunks = [c for c in chunks if isinstance(c, str)]
    text_joined = "".join(text_chunks)
    assert "first-half" in text_joined
    assert "fallback" not in text_joined


def test_streaming_keyboard_interrupt_propagates(tmp_path, monkeypatch):
    """KeyboardInterrupt must NOT be swallowed by the fall-through
    loop — Ctrl+C still works during streaming."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "fallback")

    def fake_post(url, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(KeyboardInterrupt):
        list(streaming.chat_stream(
            [{"role": "user", "content": "hi"}], model="primary",
        ))


# ============================================================
# Logged fall-through events
# ============================================================


def test_streaming_fallback_records_log_entry(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_MODEL_FALLBACK", "rescue")

    def fake_post(url, **kwargs):
        if kwargs["json"]["model"] == "primary":
            return _make_5xx_response()
        return _make_success_response("rescue")

    monkeypatch.setattr(requests, "post", fake_post)

    list(streaming.chat_stream(
        [{"role": "user", "content": "hi"}], model="primary",
    ))
    log_text = config.LOG_FILE.read_text(encoding="utf-8")
    assert "model_fallback" in log_text
    assert "primary" in log_text
    assert "rescue" in log_text
    assert "503" in log_text
