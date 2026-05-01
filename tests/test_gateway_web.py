"""Tests for Phase 11 — web gateway.

The web module lazy-imports FastAPI; tests skip cleanly when it isn't
installed. We verify:
  - host/port resolution and the non-localhost refusal rule
  - that serve() exits cleanly when FastAPI is missing
  - (when FastAPI is present) the GET / and POST /run endpoints work
"""
from __future__ import annotations
import pytest

from janus import config
from janus.gateways import web as web_mod


def test_resolve_host_localhost_default(janus_home):
    host, refusal = web_mod._resolve_host(None)
    assert host in ("127.0.0.1", "localhost", "::1")
    assert refusal is None


def test_resolve_host_explicit_localhost_ok(janus_home):
    host, refusal = web_mod._resolve_host("127.0.0.1")
    assert refusal is None


def test_resolve_host_refuses_non_localhost_without_env(janus_home, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST_OK", False)
    host, refusal = web_mod._resolve_host("0.0.0.0")
    assert host == "0.0.0.0"
    assert refusal is not None
    assert "JANUS_WEB_HOST_OK" in refusal


def test_resolve_host_allows_non_localhost_with_env(janus_home, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST_OK", True)
    host, refusal = web_mod._resolve_host("0.0.0.0")
    assert host == "0.0.0.0"
    assert refusal is None


def test_serve_without_fastapi_returns_clear_error(janus_home, monkeypatch, capsys):
    monkeypatch.setattr(web_mod, "_try_import_fastapi", lambda: None)
    rc = web_mod.serve()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FastAPI not installed" in out


def test_serve_refuses_non_localhost_without_env(janus_home, monkeypatch, capsys):
    # Stub fastapi import so we get past the dep check.
    monkeypatch.setattr(
        web_mod, "_try_import_fastapi",
        lambda: ("a", "b", "c", "d", "e", _FakeUvicorn()),
    )
    monkeypatch.setattr(config, "WEB_HOST_OK", False)
    monkeypatch.setattr(config, "API_KEY", "test")  # assert_configured() pass
    rc = web_mod.serve(host="0.0.0.0")
    out = capsys.readouterr().out
    assert rc == 2
    assert "JANUS_WEB_HOST_OK" in out


# Helper: pretend uvicorn so serve() doesn't actually start.
class _FakeUvicorn:
    def run(self, app, host=None, port=None, log_level=None):
        pass


# ---------- Live FastAPI tests (only if FastAPI installed) ----------


_HAS_FASTAPI = web_mod._try_import_fastapi() is not None


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_index_page_renders(janus_home):
    from fastapi.testclient import TestClient
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text
    assert "janus" in body.lower()
    # Concept-B SVG logo is inline in the header.
    assert "<svg" in body
    assert 'href="/favicon.svg"' in body
    # Tagline reaches the page.
    from janus import branding
    assert branding.TAGLINE in body


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_favicon_route_serves_svg(janus_home):
    from fastapi.testclient import TestClient
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/favicon.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers.get("content-type", "")
    body = r.text
    assert body.startswith("<svg")
    assert body.rstrip().endswith("</svg>")
    # Favicon uses literal brand color (browsers ignore page CSS for favicons).
    from janus import branding
    assert branding.BRAND_COLOR in body


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_run_endpoint_returns_interpretations(janus_home, fake_llm):
    """POST /run drives the interpreter; we stub the LLM."""
    from fastapi.testclient import TestClient
    fake_llm.append({"content": '{"interpretations": ['
                     '{"label": "lookup", "action": "do x", "risk": "low"}'
                     ']}'})
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/run", json={"request": "do something"})
    assert r.status_code == 200
    data = r.json()
    assert "interpretations" in data
    assert data["interpretations"][0]["label"] == "lookup"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_run_endpoint_rejects_empty_request(janus_home):
    from fastapi.testclient import TestClient
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/run", json={"request": ""})
    assert r.status_code == 200
    assert "error" in r.json()
