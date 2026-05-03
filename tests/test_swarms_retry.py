"""Tests for v1.4 retry/backoff at the llm.chat boundary.

Critical for the long-running swarm story (12hr unattended runs hit
transient HTTP 5xx and ConnectionError; without retries the swarm dies
on the first hiccup). All tests skip real network and skip real sleeps.
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest
import requests

from janus import llm


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """Skip the backoff sleep so tests run instantly."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


@pytest.fixture
def post_calls(monkeypatch):
    """Replace requests.post with a controllable mock that returns a
    queued sequence of responses (or raises queued exceptions)."""
    queue: list = []
    calls: list = []

    def _post(url, **kw):
        calls.append({"url": url, "kw": kw})
        if not queue:
            raise RuntimeError("post_calls queue empty — test forgot to enqueue")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm.requests, "post", _post)
    return {"queue": queue, "calls": calls}


def _make_response(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.json.return_value = body or {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}]
    }
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    else:
        r.raise_for_status.return_value = None
    return r


# ---------- _post_with_retry directly ----------


def test_no_retry_on_first_success(post_calls, fast_sleep):
    post_calls["queue"].append(_make_response(200))
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    assert r.status_code == 200
    assert len(post_calls["calls"]) == 1
    assert fast_sleep == []


def test_retry_on_5xx_then_success(post_calls, fast_sleep):
    post_calls["queue"].extend([
        _make_response(503),
        _make_response(503),
        _make_response(200),
    ])
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    assert r.status_code == 200
    assert len(post_calls["calls"]) == 3
    assert len(fast_sleep) == 2  # two backoffs before the third try


def test_retry_on_429(post_calls, fast_sleep):
    post_calls["queue"].extend([
        _make_response(429),
        _make_response(200),
    ])
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    assert r.status_code == 200
    assert len(post_calls["calls"]) == 2


def test_no_retry_on_4xx_other_than_429(post_calls, fast_sleep):
    post_calls["queue"].append(_make_response(401))
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    # 401 is returned as-is so caller's raise_for_status surfaces it.
    assert r.status_code == 401
    assert len(post_calls["calls"]) == 1
    assert fast_sleep == []


def test_retry_on_connection_error_then_success(post_calls, fast_sleep):
    post_calls["queue"].extend([
        requests.exceptions.ConnectionError("boom"),
        _make_response(200),
    ])
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    assert r.status_code == 200
    assert len(post_calls["calls"]) == 2
    assert len(fast_sleep) == 1


def test_retry_on_timeout_then_success(post_calls, fast_sleep):
    post_calls["queue"].extend([
        requests.exceptions.Timeout("slow"),
        _make_response(200),
    ])
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    assert r.status_code == 200


def test_exhausted_attempts_returns_last_5xx(post_calls, fast_sleep, monkeypatch):
    # MAX_ATTEMPTS=3 → 3 tries; if all 503 the last response is returned.
    monkeypatch.setattr(llm.config, "LLM_RETRY_MAX_ATTEMPTS", 3)
    post_calls["queue"].extend([
        _make_response(503),
        _make_response(503),
        _make_response(503),
    ])
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    # On exhaustion we return the last response so caller's
    # raise_for_status() can surface the actual error.
    assert r.status_code == 503
    assert len(post_calls["calls"]) == 3
    assert len(fast_sleep) == 2  # only 2 sleeps; last attempt has no follow-on sleep


def test_exhausted_attempts_raises_connection_error(post_calls, fast_sleep, monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_RETRY_MAX_ATTEMPTS", 2)
    post_calls["queue"].extend([
        requests.exceptions.ConnectionError("boom"),
        requests.exceptions.ConnectionError("boom2"),
    ])
    with pytest.raises(requests.exceptions.ConnectionError, match="boom2"):
        llm._post_with_retry(
            "http://x", headers={}, json_payload={}, timeout=10,
        )
    assert len(post_calls["calls"]) == 2


def test_retry_attempts_setting_honored(post_calls, fast_sleep, monkeypatch):
    # If MAX_ATTEMPTS=1, no retries at all.
    monkeypatch.setattr(llm.config, "LLM_RETRY_MAX_ATTEMPTS", 1)
    post_calls["queue"].append(_make_response(503))
    r = llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    assert r.status_code == 503
    assert len(post_calls["calls"]) == 1
    assert fast_sleep == []


def test_backoff_grows_exponentially(post_calls, fast_sleep, monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_RETRY_BACKOFF_BASE_S", 1.0)
    monkeypatch.setattr(llm.config, "LLM_RETRY_MAX_ATTEMPTS", 4)
    # Force jitter to 0 so we can assert the exact values.
    monkeypatch.setattr(llm.random, "uniform", lambda a, b: 0.0)
    post_calls["queue"].extend([_make_response(503)] * 4)
    llm._post_with_retry(
        "http://x", headers={}, json_payload={}, timeout=10,
    )
    # base * 2^attempt for attempt = 0, 1, 2 (last attempt has no sleep)
    assert fast_sleep == [1.0, 2.0, 4.0]


# ---------- llm.chat() integration ----------


def test_chat_uses_default_model(post_calls, fast_sleep, monkeypatch):
    monkeypatch.setattr(llm.config, "MODEL", "default-model")
    monkeypatch.setattr(llm.config, "API_KEY", "k")
    monkeypatch.setattr(llm.config, "API_BASE", "http://api")
    post_calls["queue"].append(_make_response(200))
    msg = llm.chat([{"role": "user", "content": "hi"}])
    assert msg == {"role": "assistant", "content": "ok"}
    sent_payload = post_calls["calls"][0]["kw"]["json"]
    assert sent_payload["model"] == "default-model"


def test_chat_model_override(post_calls, fast_sleep, monkeypatch):
    monkeypatch.setattr(llm.config, "MODEL", "default-model")
    monkeypatch.setattr(llm.config, "API_KEY", "k")
    monkeypatch.setattr(llm.config, "API_BASE", "http://api")
    post_calls["queue"].append(_make_response(200))
    llm.chat([{"role": "user", "content": "hi"}], model="override-haiku")
    sent_payload = post_calls["calls"][0]["kw"]["json"]
    assert sent_payload["model"] == "override-haiku"


def test_chat_retries_on_503(post_calls, fast_sleep, monkeypatch):
    monkeypatch.setattr(llm.config, "MODEL", "m")
    monkeypatch.setattr(llm.config, "API_KEY", "k")
    monkeypatch.setattr(llm.config, "API_BASE", "http://api")
    post_calls["queue"].extend([
        _make_response(503),
        _make_response(503),
        _make_response(200),
    ])
    msg = llm.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok"
    assert len(post_calls["calls"]) == 3
