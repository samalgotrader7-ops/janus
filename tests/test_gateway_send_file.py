"""Tests for v1.5.1 phase 5: gateway_send_file tool.

Bug: User asked the Telegram bot "send me that MD file to the telegram".
Bot replied "Here's the full content…" and pasted 11KB of markdown into
the chat. That's not "sending the file" — that's "showing the contents".

Root cause: no tool existed for "send this file as a Telegram document".
The model defaulted to fs_read + paste because that was the closest
fit in its tool surface.

Fix: GatewaySendFile tool registered by the gateway with a send_fn
closure over bot + chat_id. Model calls gateway_send_file(path=...) →
real Telegram document delivery.
"""
from __future__ import annotations
import inspect
from pathlib import Path

import pytest

from janus.tools.gateway_send_file import GatewaySendFile


# ---------- Tool metadata ----------


def test_tool_metadata():
    t = GatewaySendFile()
    assert t.name == "gateway_send_file"
    assert t.risk == "exec"
    assert "path" in t.parameters["properties"]
    assert "caption" in t.parameters["properties"]
    assert "path" in t.parameters["required"]


def test_tool_description_explains_use_case():
    """Description must guide the model away from fs_read+paste."""
    t = GatewaySendFile()
    desc = t.description.lower()
    assert "send" in desc
    assert "attachment" in desc
    # Negative directive: explicit "do NOT use fs_read" or similar
    assert "fs_read" in t.description or "paste" in t.description.lower()


def test_tool_schema_renders():
    t = GatewaySendFile()
    s = t.schema()
    assert s["function"]["name"] == "gateway_send_file"
    assert "description" in s["function"]


# ---------- run() — no send_fn (CLI / headless context) ----------


def test_run_returns_clear_error_when_not_in_gateway():
    """Outside a gateway (CLI, headless), send_fn is None → clear
    error so the model knows to use fs_read instead."""
    t = GatewaySendFile(send_fn=None)
    out = t.run({"path": "/some/file.md"}, lambda *a, **kw: True)
    assert "only works" in out.lower() or "gateway" in out.lower()


# ---------- run() — happy path ----------


def test_run_calls_send_fn_with_path_and_caption(tmp_path):
    f = tmp_path / "report.md"
    f.write_text("# Report", encoding="utf-8")
    calls: list = []

    def send(path, caption):
        calls.append({"path": path, "caption": caption})

    t = GatewaySendFile(send_fn=send)
    out = t.run(
        {"path": str(f), "caption": "here you go"},
        lambda *a, **kw: True,
    )
    assert "sent report.md" in out
    assert len(calls) == 1
    assert calls[0]["path"] == str(f)
    assert calls[0]["caption"] == "here you go"


def test_run_with_no_caption_passes_empty_string(tmp_path):
    f = tmp_path / "report.md"
    f.write_text("# x", encoding="utf-8")
    calls: list = []

    def send(path, caption):
        calls.append(caption)

    t = GatewaySendFile(send_fn=send)
    t.run({"path": str(f)}, lambda *a, **kw: True)
    assert calls == [""]


# ---------- run() — error paths ----------


def test_run_missing_path_errors():
    t = GatewaySendFile(send_fn=lambda p, c: None)
    out = t.run({}, lambda *a, **kw: True)
    assert "path required" in out


def test_run_path_does_not_exist_errors(tmp_path):
    t = GatewaySendFile(send_fn=lambda p, c: None)
    out = t.run({"path": str(tmp_path / "nope.md")}, lambda *a, **kw: True)
    assert "not a file" in out


def test_run_path_is_directory_errors(tmp_path):
    t = GatewaySendFile(send_fn=lambda p, c: None)
    out = t.run({"path": str(tmp_path)}, lambda *a, **kw: True)
    assert "not a file" in out


def test_run_send_exception_surfaced(tmp_path):
    f = tmp_path / "report.md"
    f.write_text("x", encoding="utf-8")

    def boom(path, caption):
        raise RuntimeError("network down")

    t = GatewaySendFile(send_fn=boom)
    out = t.run({"path": str(f)}, lambda *a, **kw: True)
    assert "send failed" in out
    assert "network down" in out


# ---------- Approver gate ----------


def test_run_refused_by_approver(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    calls: list = []
    t = GatewaySendFile(send_fn=lambda p, c: calls.append(p))
    out = t.run({"path": str(f)}, lambda *a, **kw: False)
    assert "refused" in out
    assert calls == []  # never sent


def test_run_passes_capability_triple_to_approver(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    seen = {}

    def capturing(action, details, **kw):
        seen.update(kw)
        return True

    t = GatewaySendFile(send_fn=lambda p, c: None)
    t.run({"path": str(f)}, capturing)
    cap = seen.get("capability")
    assert cap is not None
    assert cap[0] == "gateway"
    assert cap[1] == "send"


# ---------- Telegram gateway integration ----------


def test_telegram_gateway_imports_send_file_tool():
    """Verify the import + tool addition is wired in telegram.py."""
    from janus.gateways import telegram
    src = inspect.getsource(telegram._run_chat_turn)
    assert "GatewaySendFile" in src
    assert "tools.add_tool" in src


def test_telegram_gateway_passes_send_fn_with_loop_bridge():
    """Verify the gateway uses asyncio.run_coroutine_threadsafe to
    bridge the sync executor thread back to the asyncio loop."""
    from janus.gateways import telegram
    src = inspect.getsource(telegram._run_chat_turn)
    assert "run_coroutine_threadsafe" in src
    assert "send_document" in src
