"""Tests for v1.35.5 — web triggers CRUD endpoints (Phase 8.4)."""

from __future__ import annotations

import pytest


def _setup(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    fake_home = tmp_path / ".janus"
    triggers_dir = fake_home / "triggers"
    triggers_dir.mkdir(parents=True)
    # Seed two triggers
    (triggers_dir / "alpha.yaml").write_text(
        "name: alpha\nkind: cron\nwhen: '@hourly'\nenabled: true\n",
        encoding="utf-8",
    )
    (triggers_dir / "beta.yaml").write_text(
        "name: beta\nkind: cron\nwhen: '@daily'\nenabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    import importlib
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", fake_home)
    monkeypatch.setattr(config, "TRIGGERS_DIR", triggers_dir)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    monkeypatch.setattr(web_module.config, "TRIGGERS_DIR", triggers_dir)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    return TestClient(app), triggers_dir


def test_disable_existing_trigger(monkeypatch, tmp_path):
    client, td = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/triggers/alpha/disable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    text = (td / "alpha.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in text


def test_enable_existing_trigger(monkeypatch, tmp_path):
    client, td = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/triggers/beta/enable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    text = (td / "beta.yaml").read_text(encoding="utf-8")
    assert "enabled: true" in text


def test_enable_unknown_returns_404(monkeypatch, tmp_path):
    client, td = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/triggers/nonexistent/enable")
    assert resp.status_code == 404


def test_path_traversal_rejected(monkeypatch, tmp_path):
    """`..` / `/` in the name → resolved to None → 404."""
    client, td = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/triggers/..%2Fetc%2Fpasswd/enable")
    assert resp.status_code in (400, 404)


def test_delete_existing_trigger(monkeypatch, tmp_path):
    client, td = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/triggers/alpha/delete")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "alpha"
    assert not (td / "alpha.yaml").exists()


def test_delete_unknown_returns_404(monkeypatch, tmp_path):
    client, td = _setup(monkeypatch, tmp_path)
    resp = client.post("/api/triggers/nope/delete")
    assert resp.status_code == 404


def test_audit_log_records_actions(monkeypatch, tmp_path):
    """Each enable/disable/delete emits an audit_log record."""
    client, td = _setup(monkeypatch, tmp_path)
    fake_home = td.parent
    audit_path = fake_home / "audit.jsonl"

    # Patch audit_log.config.HOME so writes go to fake_home
    import importlib
    from janus import audit_log, config
    monkeypatch.setattr(audit_log.config, "HOME", fake_home)

    client.post("/api/triggers/alpha/disable")
    client.post("/api/triggers/alpha/enable")
    client.post("/api/triggers/beta/delete")

    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    actions = []
    import json
    for line in lines:
        try:
            actions.append(json.loads(line)["action"])
        except Exception:
            pass
    assert "trigger.disable" in actions
    assert "trigger.enable" in actions
    assert "trigger.delete" in actions


def test_version_bumped_to_1_35_5_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 5)
