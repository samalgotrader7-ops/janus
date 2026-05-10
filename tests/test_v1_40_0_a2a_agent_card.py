"""Tests for v1.40.0 — A2A Agent Card foundations (Phase 10.4.0)."""

from __future__ import annotations

import pytest

from janus import a2a, branding


# ---------- build_agent_card direct ----------


def test_card_has_required_fields():
    card = a2a.build_agent_card()
    assert "name" in card
    assert "description" in card
    assert "version" in card
    assert "url" in card
    assert "skills" in card
    assert "authentication" in card
    assert "capabilities" in card
    assert "defaultInputModes" in card
    assert "defaultOutputModes" in card


def test_card_version_matches_branding():
    card = a2a.build_agent_card()
    assert card["version"] == branding.VERSION


def test_card_default_name(monkeypatch):
    monkeypatch.delenv("JANUS_A2A_NAME", raising=False)
    card = a2a.build_agent_card()
    assert card["name"] == "Janus"


def test_card_env_overrides(monkeypatch):
    monkeypatch.setenv("JANUS_A2A_NAME", "MyAgent")
    monkeypatch.setenv("JANUS_A2A_URL", "https://example.com/a2a")
    monkeypatch.setenv("JANUS_A2A_DESCRIPTION", "Custom desc")
    monkeypatch.setenv("JANUS_A2A_PROVIDER", "Acme Corp")
    card = a2a.build_agent_card()
    assert card["name"] == "MyAgent"
    assert card["url"] == "https://example.com/a2a"
    assert card["description"] == "Custom desc"
    assert card["provider"]["organization"] == "Acme Corp"


def test_card_default_url_empty_when_unset(monkeypatch):
    monkeypatch.delenv("JANUS_A2A_URL", raising=False)
    card = a2a.build_agent_card()
    # URL is "" not missing — clients can detect "deployment didn't
    # configure callback URL" by checking for empty string.
    assert card["url"] == ""


def test_card_authentication_bearer_default(monkeypatch):
    monkeypatch.delenv("JANUS_A2A_AUTH", raising=False)
    card = a2a.build_agent_card()
    assert card["authentication"]["schemes"] == ["bearer"]


def test_card_authentication_none(monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "none")
    card = a2a.build_agent_card()
    assert card["authentication"]["schemes"] == []


def test_card_authentication_invalid_falls_back_to_bearer(monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "oauth2-pkce-magic")
    card = a2a.build_agent_card()
    assert card["authentication"]["schemes"] == ["bearer"]


def test_card_skills_is_list():
    card = a2a.build_agent_card()
    assert isinstance(card["skills"], list)
    assert len(card["skills"]) >= 1


def test_card_each_skill_has_required_fields():
    card = a2a.build_agent_card()
    for s in card["skills"]:
        assert "id" in s
        assert "name" in s
        assert "description" in s
        assert "tags" in s
        assert isinstance(s["tags"], list)


def test_card_capabilities_block():
    """Pin: streaming + pushNotifications declared (false in v1.40.0)
    so clients know what to expect."""
    card = a2a.build_agent_card()
    caps = card["capabilities"]
    assert caps["streaming"] is False
    assert caps["pushNotifications"] is False


def test_card_default_modes():
    card = a2a.build_agent_card()
    assert "text/plain" in card["defaultInputModes"]
    assert "text/plain" in card["defaultOutputModes"]


# ---------- auth_required helper ----------


def test_auth_required_default(monkeypatch):
    monkeypatch.delenv("JANUS_A2A_AUTH", raising=False)
    assert a2a.auth_required() is True


def test_auth_required_when_none(monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "none")
    assert a2a.auth_required() is False


def test_a2a_bearer_token_unset(monkeypatch):
    monkeypatch.delenv("JANUS_A2A_TOKEN", raising=False)
    assert a2a.a2a_bearer_token() == ""


def test_a2a_bearer_token_set(monkeypatch):
    monkeypatch.setenv("JANUS_A2A_TOKEN", "secret123")
    assert a2a.a2a_bearer_token() == "secret123"


# ---------- web endpoint ----------


_HAS_FASTAPI = True
try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
except ImportError:
    _HAS_FASTAPI = False


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_well_known_agent_returns_card(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/.well-known/agent.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Janus"
    assert body["version"] == branding.VERSION
    assert isinstance(body["skills"], list)


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_well_known_agent_is_public(janus_home):
    """Pin: discovery endpoint must NOT require auth (per A2A spec)."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    # No login — straight GET
    r = c.get("/.well-known/agent.json")
    assert r.status_code == 200


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_well_known_agent_returns_json_content_type(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/.well-known/agent.json")
    assert "json" in r.headers["content-type"].lower()


# ---------- version ----------


def test_version_bumped_to_1_40_0():
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 40, 0)
