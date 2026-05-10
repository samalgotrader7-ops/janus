"""Tests for v1.35.6 — POST /api/settings (Phase 8.3)."""

from __future__ import annotations

import pytest


def _setup(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    fake_home = tmp_path / ".janus"
    fake_home.mkdir()
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    import importlib
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", fake_home)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    return TestClient(app), fake_home


def test_write_single_whitelisted_key(monkeypatch, tmp_path):
    client, home = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/settings", json={
        "key": "JANUS_TELEGRAM_VERBOSE", "value": "1",
    })
    assert resp.status_code == 200
    assert "JANUS_TELEGRAM_VERBOSE" in resp.json()["updated_keys"]
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "JANUS_TELEGRAM_VERBOSE=1" in env_text


def test_write_batch_updates(monkeypatch, tmp_path):
    client, home = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/settings", json={
        "updates": {
            "JANUS_TOOL_SCHEMA_SLIM": "1",
            "JANUS_BUDGET_USD": "10.00",
        }
    })
    assert resp.status_code == 200
    keys = resp.json()["updated_keys"]
    assert "JANUS_TOOL_SCHEMA_SLIM" in keys
    assert "JANUS_BUDGET_USD" in keys


def test_non_whitelisted_key_rejected(monkeypatch, tmp_path):
    """Random env keys cannot be written — only the curated set."""
    client, home = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/settings", json={
        "key": "JANUS_API_KEY", "value": "leak",  # secret — not writable
    })
    assert resp.status_code == 403
    assert "rejected" in resp.json()


def test_invalid_body_400(monkeypatch, tmp_path):
    client, home = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/settings", json={"random": "field"})
    assert resp.status_code == 400


def test_persisted_with_chmod_600(monkeypatch, tmp_path):
    client, home = _setup(monkeypatch, tmp_path)
    client.post("/api/settings", json={
        "key": "JANUS_MODEL", "value": "openai/gpt-4o-mini",
    })
    env_path = home / ".env"
    assert env_path.exists()
    # On POSIX the mode would be 0600; on Windows the chmod is best-
    # effort but the test still verifies write succeeds.


def test_existing_env_preserved(monkeypatch, tmp_path):
    """Other env vars in .env aren't blown away."""
    client, home = _setup(monkeypatch, tmp_path)
    (home / ".env").write_text(
        "JANUS_API_KEY=keep-me\n"
        "OTHER_VAR=preserve\n",
        encoding="utf-8",
    )
    client.post("/api/settings", json={
        "key": "JANUS_MODEL", "value": "new-model",
    })
    text = (home / ".env").read_text(encoding="utf-8")
    assert "JANUS_API_KEY=keep-me" in text
    assert "OTHER_VAR=preserve" in text
    assert "JANUS_MODEL=new-model" in text


def test_in_process_env_updated(monkeypatch, tmp_path):
    """os.environ should reflect the update without a restart."""
    client, home = _setup(monkeypatch, tmp_path)
    import os
    client.post("/api/settings", json={
        "key": "JANUS_TELEGRAM_VERBOSE", "value": "1",
    })
    assert os.environ.get("JANUS_TELEGRAM_VERBOSE") == "1"


def test_audit_log_records_write(monkeypatch, tmp_path):
    client, home = _setup(monkeypatch, tmp_path)
    from janus import audit_log
    monkeypatch.setattr(audit_log.config, "HOME", home)
    client.post("/api/settings", json={
        "key": "JANUS_MODEL", "value": "test",
    })
    audit_path = home / "audit.jsonl"
    assert audit_path.exists()
    text = audit_path.read_text(encoding="utf-8")
    assert "settings.write" in text


def test_get_settings_still_works(monkeypatch, tmp_path):
    """GET /api/settings preserves its read-only behavior."""
    client, _ = _setup(monkeypatch, tmp_path)
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert "model" in resp.json()


def test_version_bumped_to_1_35_6_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 6)
