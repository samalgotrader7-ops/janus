"""Tests for v1.33.2 — /api/health endpoint (Phase 6.3)."""

from __future__ import annotations

from pathlib import Path

import pytest


def _build_app_unauth():
    """Build the web app with localhost auth bypass enabled so the
    auth gate doesn't block the test."""
    fastapi = pytest.importorskip("fastapi")
    import os
    os.environ["JANUS_WEB_LOCALHOST_NO_AUTH"] = "1"
    from janus.gateways import web as web_module
    # Web reads env at module load; ensure config picks it up too
    from janus import config
    import importlib
    importlib.reload(config)
    importlib.reload(web_module)
    return web_module._build_app(), web_module


def test_health_endpoint_registered():
    """Source-pin: GET /api/health route is in web.py."""
    web_path = (
        Path(__file__).parent.parent / "janus" / "gateways" / "web.py"
    )
    src = web_path.read_text(encoding="utf-8")
    assert '@app.get("/api/health")' in src
    assert "async def api_health" in src
    # v1.33.2 marker
    assert "v1.33.2" in src


def test_process_start_ts_set_at_module_load():
    """The module-level _PROCESS_START_TS is set at import time so
    uptime_seconds is meaningful."""
    web_path = (
        Path(__file__).parent.parent / "janus" / "gateways" / "web.py"
    )
    src = web_path.read_text(encoding="utf-8")
    assert "_PROCESS_START_TS" in src
    assert "_time.time()" in src or "time.time()" in src


def test_health_response_shape():
    """Behavioral: hit /api/health via TestClient and verify the
    response shape."""
    fastapi = pytest.importorskip("fastapi")
    app, web_module = _build_app_unauth()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    # Required fields
    for field in (
        "version",
        "uptime_seconds",
        "last_turn_age_seconds",
        "services_active",
        "status",
    ):
        assert field in data, f"missing field: {field}"
    # Type sanity
    assert isinstance(data["version"], str)
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0
    assert isinstance(data["services_active"], list)
    assert data["status"] in ("healthy", "degraded")


def test_health_endpoint_no_auth_required():
    """Health endpoint is conventionally public — monitoring tools
    don't carry credentials. The auth gate must NOT be applied."""
    fastapi = pytest.importorskip("fastapi")
    # Clean env, no auth bypass
    import os
    os.environ.pop("JANUS_WEB_LOCALHOST_NO_AUTH", None)
    from janus.gateways import web as web_module
    from janus import config
    import importlib
    importlib.reload(config)
    importlib.reload(web_module)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    # Without any auth cookie/token, /api/health should still 200
    resp = client.get("/api/health")
    assert resp.status_code == 200
    # And other auth-gated endpoints should NOT 200 (sanity that the
    # auth gate is actually active, just bypassed for /api/health).
    other = client.get("/api/mcp/catalog")
    # 401 or 403 — definitely not 200
    assert other.status_code != 200


def test_health_status_degraded_when_log_old(tmp_path, monkeypatch):
    """If log.jsonl mtime is > 24h ago, status should be 'degraded'."""
    pytest.importorskip("fastapi")
    import os
    import time
    fake_home = tmp_path / ".janus"
    fake_home.mkdir()
    log = fake_home / "log.jsonl"
    log.write_text("{}\n")
    # Make it 25 hours old
    old_ts = time.time() - (25 * 3600)
    os.utime(log, (old_ts, old_ts))

    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    from janus import config
    import importlib
    monkeypatch.setattr(config, "HOME", fake_home)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["last_turn_age_seconds"] is not None
    assert data["last_turn_age_seconds"] > 86400
    assert data["status"] == "degraded"


def test_health_status_healthy_when_no_log_yet(tmp_path, monkeypatch):
    """No log.jsonl (fresh install) → last_turn_age_seconds=null
    and status='healthy' (not degraded — there's nothing wrong, the
    process just started)."""
    pytest.importorskip("fastapi")
    fake_home = tmp_path / ".janus"
    fake_home.mkdir()
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    monkeypatch.setattr("janus.config.HOME", fake_home)
    from janus.gateways import web as web_module
    import importlib
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/api/health")
    data = resp.json()
    assert data["last_turn_age_seconds"] is None
    assert data["status"] == "healthy"


def test_health_returns_current_version():
    """version field reflects branding.VERSION."""
    pytest.importorskip("fastapi")
    app, web_module = _build_app_unauth()
    from fastapi.testclient import TestClient
    from janus import branding
    client = TestClient(app)
    resp = client.get("/api/health")
    data = resp.json()
    assert data["version"] == branding.VERSION


def test_version_bumped_to_1_33_2_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 33, 2)
