"""Tests for v1.5.2 phase 1: telegram_send_file + telegram_send_message
tools that work from any context when JANUS_TELEGRAM_TOKEN is set.

Bug they fix: in CLI, the model tried to call gateway_send_file (which
only exists in the Telegram gateway context) → "unknown tool" error.
Sam asked "send the .md to my telegram" from CLI; the model knew the
chat_id from session_recent but had no working send tool.
"""
from __future__ import annotations

import pytest

from janus import config
from janus.tools.telegram_send import TelegramSendFile, TelegramSendMessage


# ---------- Tool metadata ----------


def test_send_file_metadata():
    t = TelegramSendFile()
    assert t.name == "telegram_send_file"
    assert t.risk == "exec"
    assert "path" in t.parameters["properties"]
    assert "chat_id" in t.parameters["properties"]
    assert sorted(t.parameters["required"]) == ["chat_id", "path"]


def test_send_file_description_disambiguates_from_gateway_send_file():
    """The description must explicitly tell the model how this differs
    from gateway_send_file so it picks the right tool per context."""
    t = TelegramSendFile()
    desc = t.description.lower()
    assert "telegram" in desc
    # Disambiguation
    assert "gateway_send_file" in t.description
    # CLI / headless context mention
    assert "cli" in desc or "headless" in desc


def test_send_message_metadata():
    t = TelegramSendMessage()
    assert t.name == "telegram_send_message"
    assert t.risk == "exec"
    assert "text" in t.parameters["properties"]
    assert "chat_id" in t.parameters["properties"]
    assert sorted(t.parameters["required"]) == ["chat_id", "text"]


def test_send_message_description_warns_against_in_chat_use():
    """Don't use this to reply to the current user — that's already
    handled by the assistant message delivery."""
    t = TelegramSendMessage()
    desc = t.description.lower()
    # Should mention "out of band" or similar
    assert "out-of-band" in desc or "out of band" in desc or "different chat" in desc


# ---------- Token gating ----------


def test_send_file_no_token_returns_clear_error(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    t = TelegramSendFile()
    out = t.run(
        {"path": "/tmp/x.md", "chat_id": "123"},
        lambda *a, **kw: True,
    )
    assert "JANUS_TELEGRAM_TOKEN is not set" in out


def test_send_message_no_token_returns_clear_error(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    t = TelegramSendMessage()
    out = t.run({"text": "hi", "chat_id": "123"}, lambda *a, **kw: True)
    assert "JANUS_TELEGRAM_TOKEN is not set" in out


# ---------- Argument validation ----------


def test_send_file_missing_path(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    t = TelegramSendFile()
    out = t.run({"chat_id": "1"}, lambda *a, **kw: True)
    assert "path required" in out


def test_send_file_missing_chat_id(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    t = TelegramSendFile()
    out = t.run({"path": "/tmp/x"}, lambda *a, **kw: True)
    assert "chat_id required" in out
    # Also hints how to find it
    assert "session_recent" in out


def test_send_file_path_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    t = TelegramSendFile()
    out = t.run(
        {"path": str(tmp_path / "nope.md"), "chat_id": "1"},
        lambda *a, **kw: True,
    )
    assert "not a file" in out


def test_send_message_missing_text(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    t = TelegramSendMessage()
    out = t.run({"chat_id": "1"}, lambda *a, **kw: True)
    assert "text required" in out


def test_send_message_missing_chat_id(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    t = TelegramSendMessage()
    out = t.run({"text": "hi"}, lambda *a, **kw: True)
    assert "chat_id required" in out


# ---------- HTTP call ----------


@pytest.fixture
def captured_post(monkeypatch):
    calls: list = []

    class FakeResp:
        def __init__(self, status=200, text=""):
            self.status_code = status
            self.text = text

    def _post(url, **kw):
        calls.append({"url": url, "kw": kw})
        return FakeResp(200)

    import requests
    monkeypatch.setattr(requests, "post", _post)
    return calls


def test_send_file_posts_to_send_document(monkeypatch, tmp_path, captured_post):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "abc-token")
    f = tmp_path / "report.md"
    f.write_text("# Report", encoding="utf-8")

    t = TelegramSendFile()
    out = t.run(
        {"path": str(f), "chat_id": "999", "caption": "here you go"},
        lambda *a, **kw: True,
    )
    assert "sent report.md" in out
    assert "999" in out

    call = captured_post[0]
    assert "abc-token" in call["url"]
    assert "sendDocument" in call["url"]
    data = call["kw"]["data"]
    assert data["chat_id"] == "999"
    assert data["caption"] == "here you go"
    # File attached
    assert "files" in call["kw"]
    assert "document" in call["kw"]["files"]


def test_send_file_no_caption(monkeypatch, tmp_path, captured_post):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")

    TelegramSendFile().run(
        {"path": str(f), "chat_id": "1"},
        lambda *a, **kw: True,
    )
    assert captured_post[0]["kw"]["data"]["caption"] == ""


def test_send_message_posts_to_send_message(monkeypatch, captured_post):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    out = TelegramSendMessage().run(
        {"text": "hello world", "chat_id": "42"},
        lambda *a, **kw: True,
    )
    assert "sent message" in out
    call = captured_post[0]
    assert "sendMessage" in call["url"]
    payload = call["kw"]["json"]
    assert payload["chat_id"] == "42"
    assert payload["text"] == "hello world"
    assert payload["parse_mode"] == "Markdown"


def test_send_message_with_html_parse_mode(monkeypatch, captured_post):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    TelegramSendMessage().run(
        {"text": "<b>bold</b>", "chat_id": "1", "parse_mode": "HTML"},
        lambda *a, **kw: True,
    )
    assert captured_post[0]["kw"]["json"]["parse_mode"] == "HTML"


def test_send_message_truncates_long_text(monkeypatch, captured_post):
    """Telegram API caps messages at 4096 chars."""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    long_text = "x" * 5000
    TelegramSendMessage().run(
        {"text": long_text, "chat_id": "1"},
        lambda *a, **kw: True,
    )
    assert len(captured_post[0]["kw"]["json"]["text"]) == 4096


# ---------- Error paths ----------


def test_send_file_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")

    class FakeResp:
        status_code = 400
        text = '{"ok":false,"description":"chat not found"}'

    def _post(url, **kw):
        return FakeResp()

    import requests
    monkeypatch.setattr(requests, "post", _post)

    out = TelegramSendFile().run(
        {"path": str(f), "chat_id": "bad"},
        lambda *a, **kw: True,
    )
    assert "Telegram API returned 400" in out
    assert "chat not found" in out


def test_send_file_network_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")

    import requests

    def _post(url, **kw):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr(requests, "post", _post)

    out = TelegramSendFile().run(
        {"path": str(f), "chat_id": "1"},
        lambda *a, **kw: True,
    )
    assert "network" in out
    assert "ConnectionError" in out


# ---------- Approver ----------


def test_send_file_refused_by_approver(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    out = TelegramSendFile().run(
        {"path": str(f), "chat_id": "1"},
        lambda *a, **kw: False,
    )
    assert "refused" in out


def test_send_file_capability_triple_passed(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    seen = {}
    TelegramSendFile().run(
        {"path": str(f), "chat_id": "999"},
        lambda action, details, **kw: (seen.update(kw), True)[1],
    )
    cap = seen.get("capability")
    assert cap == ("telegram", "send_file", "999")


def test_send_message_capability_triple_passed(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    seen = {}
    TelegramSendMessage().run(
        {"text": "hi", "chat_id": "555"},
        lambda action, details, **kw: (seen.update(kw), True)[1],
    )
    cap = seen.get("capability")
    assert cap == ("telegram", "send_message", "555")


# ---------- Registry inclusion ----------


def test_telegram_tools_in_default_registry():
    """Both tools should be available in default_registry from any context."""
    from janus.tools import default_registry
    reg = default_registry()
    names = reg.names()
    assert "telegram_send_file" in names
    assert "telegram_send_message" in names


def test_telegram_send_file_schema_in_default_registry():
    from janus.tools import default_registry
    reg = default_registry()
    names = [s["function"]["name"] for s in reg.schemas()]
    assert "telegram_send_file" in names
    assert "telegram_send_message" in names
