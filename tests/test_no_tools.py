"""tests/test_no_tools.py — v1.16.2 JANUS_NO_TOOLS fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest
import requests

from janus import config, llm, streaming


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()


def _fake_response(status: int = 200, body: dict | None = None) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.json = MagicMock(return_value=body or {})
    r.text = ""
    r.raise_for_status = MagicMock()
    return r


# ============================================================
# Payload construction respects JANUS_NO_TOOLS
# ============================================================


def test_chat_strips_tools_when_no_tools_flag_set(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "MODEL", "m")
    monkeypatch.setattr(config, "NO_TOOLS", True)

    captured: dict = {}

    def fake_post(url, **kw):
        captured["payload"] = kw.get("json_payload") or kw.get("json")
        return _fake_response(200, {
            "choices": [{"message": {"content": "ok"}}]
        })

    monkeypatch.setattr(llm, "_post_with_retry", fake_post)

    llm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "foo"}}],
    )
    assert "tools" not in captured["payload"]
    assert "tool_choice" not in captured["payload"]


def test_chat_keeps_tools_when_no_tools_flag_off(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "MODEL", "m")
    monkeypatch.setattr(config, "NO_TOOLS", False)

    captured: dict = {}

    def fake_post(url, **kw):
        captured["payload"] = kw.get("json_payload") or kw.get("json")
        return _fake_response(200, {
            "choices": [{"message": {"content": "ok"}}]
        })

    monkeypatch.setattr(llm, "_post_with_retry", fake_post)

    llm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "foo"}}],
    )
    assert captured["payload"].get("tools")
    assert captured["payload"].get("tool_choice") == "auto"


def test_streaming_strips_tools_when_no_tools_flag(tmp_path, monkeypatch):
    """Streaming path also respects NO_TOOLS."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "MODEL", "m")
    monkeypatch.setattr(config, "NO_TOOLS", True)

    captured: dict = {}

    class _CtxResponse:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            # Minimal valid SSE stream
            yield 'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}'
            yield 'data: [DONE]'

    def fake_post(url, **kw):
        captured["payload"] = kw.get("json_payload") or kw.get("json")
        return _CtxResponse()

    monkeypatch.setattr(llm, "_post_with_retry", fake_post)

    out = list(streaming.chat_stream(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "foo"}}],
    ))
    assert "tools" not in captured["payload"]


# ============================================================
# 404 message suggests JANUS_NO_TOOLS=1 when tools were sent
# ============================================================


def test_explain_404_suggests_no_tools_when_had_tools():
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 404
    fake.json = MagicMock(side_effect=ValueError())
    fake.text = ""
    err = llm._explain_404(
        "Qwen/Q-7B", "https://my-vllm/v1", fake, had_tools=True,
    )
    msg = str(err)
    assert "JANUS_NO_TOOLS=1" in msg
    assert "enable-auto-tool-choice" in msg


def test_explain_404_no_no_tools_suggestion_without_tools():
    """When the failed request HAD NO tools, don't suggest JANUS_NO_TOOLS=1
    (it wouldn't help)."""
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 404
    fake.json = MagicMock(side_effect=ValueError())
    fake.text = ""
    err = llm._explain_404(
        "Qwen/Q-7B", "https://my-vllm/v1", fake, had_tools=False,
    )
    msg = str(err)
    assert "JANUS_NO_TOOLS" not in msg


def test_chat_404_passes_had_tools_to_explain(tmp_path, monkeypatch):
    """End-to-end: chat with tools, 404 response, error mentions NO_TOOLS."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "API_BASE", "https://my-vllm.com/v1")
    monkeypatch.setattr(config, "MODEL", "m")
    monkeypatch.setattr(config, "NO_TOOLS", False)

    fake = _fake_response(404, {})
    monkeypatch.setattr(llm, "_post_with_retry", lambda *a, **kw: fake)

    with pytest.raises(RuntimeError) as exc_info:
        llm.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "foo"}}],
        )
    msg = str(exc_info.value)
    assert "JANUS_NO_TOOLS=1" in msg


def test_chat_404_without_tools_does_not_mention_no_tools(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "API_BASE", "https://my-vllm.com/v1")
    monkeypatch.setattr(config, "MODEL", "m")
    monkeypatch.setattr(config, "NO_TOOLS", False)

    fake = _fake_response(404, {})
    monkeypatch.setattr(llm, "_post_with_retry", lambda *a, **kw: fake)

    with pytest.raises(RuntimeError) as exc_info:
        llm.chat([{"role": "user", "content": "hi"}], tools=None)
    msg = str(exc_info.value)
    # No tools were sent → the suggestion would mislead, so it's omitted.
    assert "JANUS_NO_TOOLS" not in msg
