"""Tests for v1.34.7 — /api/logs filter parameters (Phase 8.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _setup_app(monkeypatch, tmp_path, log_lines: list[dict]):
    """Build the FastAPI app with auth bypass + a synthetic log.jsonl."""
    pytest.importorskip("fastapi")
    fake_home = tmp_path / ".janus"
    fake_home.mkdir()
    log = fake_home / "log.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in log_lines), encoding="utf-8")

    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    import importlib
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", fake_home)
    monkeypatch.setattr(config, "LOG_FILE", log)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    monkeypatch.setattr(web_module.config, "LOG_FILE", log)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def sample_log():
    return [
        {"ts": "2026-05-01T10:00:00Z", "model": "anthropic/claude-haiku-4-5",
         "mode": "default", "request": "list files in cwd"},
        {"ts": "2026-05-02T10:00:00Z", "model": "openai/gpt-4o-mini",
         "mode": "plan", "request": "review the diff against main"},
        {"ts": "2026-05-03T10:00:00Z", "model": "anthropic/claude-sonnet-4-6",
         "mode": "acceptEdits", "request": "fix the failing test"},
        {"ts": "2026-05-04T10:00:00Z", "model": "openrouter/llama-3-8b",
         "mode": "default", "request": "deploy to staging"},
    ]


# -------------------- API behavior --------------------


def test_no_filters_returns_all(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 4


def test_filter_by_mode(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?mode=default")
    entries = resp.json()["entries"]
    assert len(entries) == 2
    assert all(e["mode"] == "default" for e in entries)


def test_filter_by_model_substring(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?model=claude")
    entries = resp.json()["entries"]
    assert len(entries) == 2
    assert all("claude" in e["model"].lower() for e in entries)


def test_filter_by_query_in_request(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?q=diff")
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert "diff" in entries[0]["request"]


def test_filter_query_is_case_insensitive(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?q=DIFF")
    entries = resp.json()["entries"]
    assert len(entries) == 1


def test_filter_by_since(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?since=2026-05-03T00:00:00Z")
    entries = resp.json()["entries"]
    assert len(entries) == 2
    assert all(e["ts"] >= "2026-05-03" for e in entries)


def test_filter_by_until(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?until=2026-05-02T23:59:59Z")
    entries = resp.json()["entries"]
    assert len(entries) == 2
    assert all(e["ts"] <= "2026-05-02T23:59:59Z" for e in entries)


def test_filters_are_and_combined(monkeypatch, tmp_path, sample_log):
    """mode=default AND model=anthropic should match only one entry."""
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?mode=default&model=anthropic")
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["mode"] == "default"
    assert "anthropic" in entries[0]["model"].lower()


def test_no_matches_returns_empty(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?q=does-not-exist-anywhere")
    entries = resp.json()["entries"]
    assert entries == []


def test_results_are_most_recent_first(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs")
    entries = resp.json()["entries"]
    timestamps = [e["ts"] for e in entries]
    assert timestamps == sorted(timestamps, reverse=True)


def test_limit_caps_results(monkeypatch, tmp_path, sample_log):
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    resp = client.get("/api/logs?limit=2")
    entries = resp.json()["entries"]
    assert len(entries) == 2


def test_limit_clamped_to_max(monkeypatch, tmp_path, sample_log):
    """limit > 1000 clamps to 1000; limit < 1 clamps to 1."""
    client = _setup_app(monkeypatch, tmp_path, sample_log)
    # Both should respond 200 (no crash)
    assert client.get("/api/logs?limit=999999").status_code == 200
    assert client.get("/api/logs?limit=0").status_code == 200


def test_malformed_log_line_handled_gracefully(monkeypatch, tmp_path):
    """Malformed JSON lines don't crash the endpoint."""
    pytest.importorskip("fastapi")
    fake_home = tmp_path / ".janus"
    fake_home.mkdir()
    log = fake_home / "log.jsonl"
    log.write_text(
        '{"ts":"2026-05-01T00:00:00Z","mode":"default","request":"good"}\n'
        'this line is not json\n'
        '{"ts":"2026-05-02T00:00:00Z","mode":"default","request":"also good"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    import importlib
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", fake_home)
    monkeypatch.setattr(config, "LOG_FILE", log)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    monkeypatch.setattr(web_module.config, "LOG_FILE", log)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/api/logs")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    # Two valid + one raw passthrough (when no filter active)
    assert len(entries) == 3


# -------------------- Source pins --------------------


def test_endpoint_accepts_filter_query_params():
    web_path = (
        Path(__file__).parent.parent / "janus" / "gateways" / "web.py"
    )
    src = web_path.read_text(encoding="utf-8")
    sig_idx = src.index("async def api_logs(")
    sig_block = src[sig_idx: sig_idx + 800]
    for param in ("since", "until", "mode", "model", "q"):
        assert f"{param}:" in sig_block, f"signature missing param: {param}"


def test_html_has_filter_inputs():
    html_path = (
        Path(__file__).parent.parent / "janus" / "gateways" / "static" / "index.html"
    )
    src = html_path.read_text(encoding="utf-8")
    for ident in (
        "logs-q", "logs-mode", "logs-model",
        "logs-since", "logs-until", "logs-apply", "logs-clear",
    ):
        assert ident in src, f"index.html missing element: {ident}"


def test_app_js_has_filter_handlers():
    js_path = (
        Path(__file__).parent.parent / "janus" / "gateways" / "static" / "app.js"
    )
    src = js_path.read_text(encoding="utf-8")
    assert "_logsApplyFilters" in src
    assert "_logsHasActiveFilter" in src
    assert "_logsBuildQuery" in src
    # When a filter is active the SSE stream is paused
    assert "_logsCloseStream" in src
    # v1.34.7 marker
    assert "v1.34.7" in src


# -------------------- Version pin --------------------


def test_version_bumped_to_1_34_7_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 34, 7)
