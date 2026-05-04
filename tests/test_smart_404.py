"""tests/test_smart_404.py — v1.16.1 actionable 404 errors + doctor probe."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest
import requests

from janus import config, llm, doctor


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()


# ============================================================
# _explain_404 — message construction
# ============================================================


def _fake_response(status: int = 404, body: dict | str | None = None,
                   text: str = "") -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    if body is not None:
        r.json = MagicMock(return_value=body)
    else:
        r.json = MagicMock(side_effect=ValueError("no json"))
    r.text = text
    return r


def test_explain_404_includes_url_and_model():
    r = _fake_response()
    err = llm._explain_404(
        "Qwen/Qwen3-7B", "https://example.proxy.runpod.net/v1", r,
    )
    msg = str(err)
    assert "404" in msg
    assert "Qwen/Qwen3-7B" in msg
    assert "https://example.proxy.runpod.net/v1/chat/completions" in msg


def test_explain_404_suggests_dropping_openai_prefix():
    """The exact bug Sam hit: 'openai/Qwen/...' on a non-OpenRouter endpoint."""
    r = _fake_response()
    err = llm._explain_404(
        "openai/Qwen/Qwen3.6-35B-A3B",
        "https://oicge6rw7x4838-8000.proxy.runpod.net/v1", r,
    )
    msg = str(err)
    assert "openai" in msg.lower()
    assert "JANUS_MODEL=Qwen/Qwen3.6-35B-A3B" in msg


def test_explain_404_does_not_suggest_dropping_for_openrouter():
    """OpenRouter genuinely uses 'provider/model' namespace; don't suggest
    stripping the prefix for it."""
    r = _fake_response()
    err = llm._explain_404(
        "openai/gpt-4o-mini",
        "https://openrouter.ai/api/v1", r,
    )
    msg = str(err)
    # Should NOT suggest "JANUS_MODEL=gpt-4o-mini" because OR uses prefixes
    assert "JANUS_MODEL=gpt-4o-mini" not in msg


def test_explain_404_includes_curl_models_hint():
    r = _fake_response()
    err = llm._explain_404("model", "https://x/v1", r)
    msg = str(err)
    assert "curl https://x/v1/models" in msg


def test_explain_404_includes_server_error_message():
    """When the server returns a JSON {error: {message: ...}}, surface it."""
    r = _fake_response(body={"error": {"message": "model not found: X"}})
    err = llm._explain_404("X", "https://x/v1", r)
    msg = str(err)
    assert "model not found: X" in msg


def test_explain_404_falls_back_to_text_when_json_fails():
    r = _fake_response(text="raw text body")
    err = llm._explain_404("X", "https://x/v1", r)
    assert "raw text body" in str(err)


def test_explain_404_handles_non_dict_error():
    """Some servers return {error: "string"} not {error: {message: ...}}."""
    r = _fake_response(body={"error": "model not loaded"})
    err = llm._explain_404("X", "https://x/v1", r)
    assert "model not loaded" in str(err)


# ============================================================
# llm.chat raises the smart error on 404
# ============================================================


def test_chat_raises_smart_error_on_404(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "API_BASE", "https://my-vllm.com/v1")
    monkeypatch.setattr(config, "MODEL", "openai/Foo/Bar")

    fake = _fake_response(status=404, body={"error": {"message": "no such model"}})
    monkeypatch.setattr(llm, "_post_with_retry", lambda *a, **kw: fake)

    with pytest.raises(RuntimeError) as exc_info:
        llm.chat([{"role": "user", "content": "hi"}])
    msg = str(exc_info.value)
    assert "404" in msg
    assert "JANUS_MODEL=Foo/Bar" in msg
    assert "no such model" in msg


def test_chat_does_not_explain_on_200(tmp_path, monkeypatch):
    """Sanity: 2xx responses don't go through the 404 path."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "MODEL", "m")
    fake = _fake_response(status=200, body={
        "choices": [{"message": {"content": "ok"}}]
    })
    fake.raise_for_status = lambda: None
    monkeypatch.setattr(llm, "_post_with_retry", lambda *a, **kw: fake)

    out = llm.chat([{"role": "user", "content": "hi"}])
    assert out["content"] == "ok"


# ============================================================
# llm.list_models — endpoint probe
# ============================================================


def test_list_models_returns_ids_from_openai_shape(monkeypatch):
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 200
    fake.json.return_value = {
        "data": [
            {"id": "Qwen/Qwen3-7B", "object": "model"},
            {"id": "meta-llama/Llama-3-8B"},
        ]
    }
    monkeypatch.setattr("requests.get", lambda *a, **kw: fake)
    out = llm.list_models(api_base="https://x/v1", api_key="k")
    assert out == ["Qwen/Qwen3-7B", "meta-llama/Llama-3-8B"]


def test_list_models_handles_models_key():
    """Some servers use {'models': [...]} instead of {'data': [...]}."""
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 200
    fake.json.return_value = {"models": [{"name": "alpha"}, {"name": "beta"}]}
    with patch("requests.get", lambda *a, **kw: fake):
        out = llm.list_models(api_base="https://x/v1", api_key="k")
    assert out == ["alpha", "beta"]


def test_list_models_handles_top_level_list():
    """Some servers (Ollama) return a top-level array."""
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 200
    fake.json.return_value = [{"id": "a"}, {"id": "b"}]
    with patch("requests.get", lambda *a, **kw: fake):
        out = llm.list_models(api_base="https://x/v1", api_key="k")
    assert out == ["a", "b"]


def test_list_models_handles_string_array():
    """Bare string array."""
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 200
    fake.json.return_value = ["a", "b", "c"]
    with patch("requests.get", lambda *a, **kw: fake):
        out = llm.list_models(api_base="https://x/v1", api_key="k")
    assert out == ["a", "b", "c"]


def test_list_models_returns_empty_on_404():
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 404
    with patch("requests.get", lambda *a, **kw: fake):
        out = llm.list_models(api_base="https://x/v1", api_key="k")
    assert out == []


def test_list_models_returns_empty_on_network_error(monkeypatch):
    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError("dns fail")
    monkeypatch.setattr("requests.get", boom)
    out = llm.list_models(api_base="https://x/v1", api_key="k")
    assert out == []


def test_list_models_returns_empty_on_malformed_body(monkeypatch):
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 200
    fake.json.side_effect = ValueError("not json")
    monkeypatch.setattr("requests.get", lambda *a, **kw: fake)
    out = llm.list_models(api_base="https://x/v1", api_key="k")
    assert out == []


# ============================================================
# doctor _check_model_reachable
# ============================================================


def test_doctor_skips_when_no_api_key(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "")
    r = doctor._check_model_reachable()
    assert r.status == "warn"
    assert "skipped" in r.message


def test_doctor_warns_when_models_unreachable(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "API_BASE", "https://x/v1")
    monkeypatch.setattr(config, "MODEL", "anything")
    monkeypatch.setattr(llm, "list_models", lambda **kw: [])
    r = doctor._check_model_reachable()
    assert r.status == "warn"


def test_doctor_passes_when_model_in_list(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "API_BASE", "https://x/v1")
    monkeypatch.setattr(config, "MODEL", "Qwen/Q-7B")
    monkeypatch.setattr(llm, "list_models", lambda **kw: ["Qwen/Q-7B", "other"])
    r = doctor._check_model_reachable()
    assert r.status == "pass"


def test_doctor_fails_with_prefix_drop_suggestion(tmp_path, monkeypatch):
    """Sam's exact case: openai/Qwen/X configured, endpoint serves Qwen/X."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "API_BASE", "https://x/v1")
    monkeypatch.setattr(config, "MODEL", "openai/Qwen/Q-7B")
    monkeypatch.setattr(llm, "list_models", lambda **kw: ["Qwen/Q-7B"])
    r = doctor._check_model_reachable()
    assert r.status == "fail"
    assert "JANUS_MODEL=Qwen/Q-7B" in r.fix


def test_doctor_fails_with_available_list_when_no_prefix(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "API_KEY", "x")
    monkeypatch.setattr(config, "API_BASE", "https://x/v1")
    monkeypatch.setattr(config, "MODEL", "totally-wrong")
    monkeypatch.setattr(llm, "list_models", lambda **kw: ["a", "b", "c"])
    r = doctor._check_model_reachable()
    assert r.status == "fail"
    assert "available" in r.fix
