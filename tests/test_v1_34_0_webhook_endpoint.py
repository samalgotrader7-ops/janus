"""Tests for v1.34.0 — generic incoming webhook endpoint (Phase 7.5)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from janus import webhooks


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / ".janus"
    home.mkdir()
    from janus import config
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(webhooks.config, "HOME", home)
    return home


def _write_config(home, payload):
    (home / "webhooks.json").write_text(json.dumps(payload))


# -------------------- load_configs --------------------


def test_load_configs_empty_when_no_file(fake_home):
    assert webhooks.load_configs() == {}


def test_load_configs_parses_valid(fake_home):
    _write_config(fake_home, {
        "github-pr": {
            "secret": "s3cret",
            "prompt_template": "PR opened: {{title}}",
            "default_mode": "default",
        }
    })
    cfgs = webhooks.load_configs()
    assert "github-pr" in cfgs
    cfg = cfgs["github-pr"]
    assert cfg.secret == "s3cret"
    assert "PR opened" in cfg.prompt_template


def test_load_configs_skips_entries_without_secret(fake_home):
    _write_config(fake_home, {
        "ok": {"secret": "s", "prompt_template": "t"},
        "bad": {"prompt_template": "t"},  # no secret
    })
    cfgs = webhooks.load_configs()
    assert "ok" in cfgs
    assert "bad" not in cfgs


def test_load_configs_handles_malformed_json(fake_home):
    (fake_home / "webhooks.json").write_text("{ this is not json")
    assert webhooks.load_configs() == {}


# -------------------- HMAC verification --------------------


def test_expected_signature_format():
    sig = webhooks.expected_signature("secret", b"hello")
    assert sig.startswith("sha256=")
    # Should match Python's hmac directly
    expected_digest = hmac.new(
        b"secret", b"hello", hashlib.sha256
    ).hexdigest()
    assert sig == f"sha256={expected_digest}"


def test_verify_signature_accepts_match():
    secret = "s3cret"
    body = b'{"x":1}'
    sig = webhooks.expected_signature(secret, body)
    assert webhooks.verify_signature(secret, body, sig) is True


def test_verify_signature_rejects_mismatch():
    secret = "s3cret"
    body = b'{"x":1}'
    bad = "sha256=" + "0" * 64
    assert webhooks.verify_signature(secret, body, bad) is False


def test_verify_signature_rejects_missing_header():
    assert webhooks.verify_signature("s", b"body", None) is False
    assert webhooks.verify_signature("s", b"body", "") is False


def test_verify_signature_constant_time():
    """The implementation uses hmac.compare_digest — pin this so a
    future refactor doesn't accidentally introduce a == compare
    that's vulnerable to timing attacks."""
    src = Path(webhooks.__file__).read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src


# -------------------- render_prompt --------------------


def test_render_prompt_substitutes_vars():
    out = webhooks.render_prompt(
        "Hello {{name}}, welcome to {{place}}",
        {"name": "Sam", "place": "Janus"},
    )
    assert out == "Hello Sam, welcome to Janus"


def test_render_prompt_dotted_path():
    out = webhooks.render_prompt(
        "PR by {{author.login}}: {{pr.title}}",
        {"author": {"login": "alice"}, "pr": {"title": "Fix bug"}},
    )
    assert out == "PR by alice: Fix bug"


def test_render_prompt_missing_keys_empty_string():
    out = webhooks.render_prompt(
        "X={{missing}} Y={{also.missing}}",
        {},
    )
    assert out == "X= Y="


def test_render_prompt_empty_template_dumps_payload():
    out = webhooks.render_prompt("", {"a": 1, "b": 2})
    assert "a" in out
    assert "1" in out


def test_render_prompt_handles_none_payload():
    out = webhooks.render_prompt("X={{x}}", None)
    assert out == "X="


def test_render_prompt_array_index():
    out = webhooks.render_prompt(
        "First: {{items.0}}",
        {"items": ["a", "b", "c"]},
    )
    assert out == "First: a"


# -------------------- evaluate() --------------------


def test_evaluate_unknown_key(fake_home):
    result = webhooks.evaluate("not-configured", b"{}", "sha256=anything")
    assert result.ok is False
    assert result.status == "unknown_key"


def test_evaluate_bad_signature(fake_home):
    _write_config(fake_home, {
        "k": {"secret": "real-secret", "prompt_template": "T"}
    })
    result = webhooks.evaluate("k", b"{}", "sha256=" + "0" * 64)
    assert result.ok is False
    assert result.status == "bad_signature"


def test_evaluate_fires_on_valid(fake_home):
    _write_config(fake_home, {
        "k": {"secret": "s3cret", "prompt_template": "name={{name}}"}
    })
    body = b'{"name":"alice"}'
    sig = webhooks.expected_signature("s3cret", body)
    result = webhooks.evaluate("k", body, sig)
    assert result.ok is True
    assert result.status == "fired"
    assert result.rendered_prompt == "name=alice"


def test_evaluate_with_invalid_json_body(fake_home):
    """Non-JSON body still works — empty payload renders empty
    template vars."""
    _write_config(fake_home, {
        "k": {"secret": "s", "prompt_template": "x={{x}}"}
    })
    body = b"not json"
    sig = webhooks.expected_signature("s", body)
    result = webhooks.evaluate("k", body, sig)
    assert result.ok is True
    assert result.status == "fired"
    assert result.rendered_prompt == "x="


# -------------------- Web endpoint behavioral --------------------


def test_webhook_endpoint_404_on_unknown_key(fake_home, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    monkeypatch.setattr(webhooks.config, "HOME", fake_home)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.post(
        "/api/webhook/nonexistent",
        content=b"{}",
        headers={"X-Janus-Signature": "sha256=" + "0" * 64},
    )
    assert resp.status_code == 404


def test_webhook_endpoint_401_on_bad_signature(fake_home, monkeypatch):
    pytest.importorskip("fastapi")
    _write_config(fake_home, {
        "k": {"secret": "s3cret", "prompt_template": "T"}
    })
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    monkeypatch.setattr(webhooks.config, "HOME", fake_home)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.post(
        "/api/webhook/k",
        content=b'{"x":1}',
        headers={"X-Janus-Signature": "sha256=" + "0" * 64},
    )
    assert resp.status_code == 401


def test_webhook_endpoint_202_on_valid(fake_home, monkeypatch):
    pytest.importorskip("fastapi")
    _write_config(fake_home, {
        "k": {"secret": "s3cret", "prompt_template": "name={{name}}"}
    })
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    # Permissive rate limit so test doesn't get 429
    monkeypatch.setenv("JANUS_RATE_LIMIT_BURST", "100")
    import importlib
    from janus import config, web_rate_limit
    importlib.reload(config)
    web_rate_limit.reset_default_limiter()
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    monkeypatch.setattr(web_module.config, "HOME", fake_home)
    monkeypatch.setattr(webhooks.config, "HOME", fake_home)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    body = b'{"name":"alice"}'
    sig = webhooks.expected_signature("s3cret", body)
    resp = client.post(
        "/api/webhook/k",
        content=body,
        headers={"X-Janus-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert "alice" in data["prompt_preview"]


# -------------------- Source pin --------------------


def test_web_endpoint_registered():
    web_path = (
        Path(webhooks.__file__).parent / "gateways" / "web.py"
    )
    src = web_path.read_text(encoding="utf-8")
    assert '@app.post("/api/webhook/{key}")' in src
    assert "async def api_webhook" in src
    assert "v1.34.0" in src


# -------------------- Version pin --------------------


def test_version_bumped_to_1_34_0_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 34, 0)
