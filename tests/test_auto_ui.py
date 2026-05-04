"""Tests for v1.5 auto-mode UI surface (system prompt + /mode listings)."""
from __future__ import annotations
import io
from contextlib import redirect_stdout

import pytest

from janus import executor, permissions
from janus.tools import default_registry
from janus.tools.capabilities import CapabilitySet


# ---------- System prompt ----------


def test_system_prompt_default_mode_no_special_text():
    msg = executor._build_chat_system(
        workspace="/tmp", mode="default", memory_preamble="",
        skill_body="", tool_count=0, skill_count=0,
    )
    assert "AUTO mode" not in msg
    assert "PLAN mode" not in msg
    assert "BYPASS mode" not in msg


def test_system_prompt_auto_mode_explains_safety():
    msg = executor._build_chat_system(
        workspace="/tmp", mode="auto", memory_preamble="",
        skill_body="", tool_count=0, skill_count=0,
    )
    assert "AUTO mode" in msg
    assert "blocks dangerous calls" in msg
    # Mentions specific examples so the model knows what to expect
    assert "rm -rf" in msg or "/etc/" in msg
    # Tells the model how to react to refusals
    assert "narrower" in msg.lower() or "different approach" in msg.lower()


def test_system_prompt_auto_mode_warns_about_injection():
    msg = executor._build_chat_system(
        workspace="/tmp", mode="auto", memory_preamble="",
        skill_body="", tool_count=0, skill_count=0,
    )
    # Auto mode should set expectations about injection warnings
    assert "injection" in msg.lower() or "warning header" in msg.lower()


def test_system_prompt_plan_mode_unchanged():
    """Verify the PLAN explanation didn't regress."""
    msg = executor._build_chat_system(
        workspace="/tmp", mode="plan", memory_preamble="",
        skill_body="", tool_count=0, skill_count=0,
    )
    assert "PLAN mode" in msg
    assert "denied" in msg.lower()


def test_system_prompt_bypass_mode_unchanged():
    msg = executor._build_chat_system(
        workspace="/tmp", mode="bypassPermissions", memory_preamble="",
        skill_body="", tool_count=0, skill_count=0,
    )
    assert "BYPASS mode" in msg


# ---------- /mode listing ----------


def test_cli_basic_mode_listing_includes_auto(janus_home, monkeypatch):
    """`/mode` (no arg) in the basic CLI shows auto in the rows table."""
    from janus import cli
    monkeypatch.setattr(cli, "_RUN_STATE", {
        "mode_state": permissions.ModeState(current="default"),
    })
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_mode("")
    output = buf.getvalue()
    assert "auto" in output
    assert "rm -rf" in output or "SSRF" in output
    assert "no safety net" in output  # bypass got the new descriptor


def test_cli_basic_mode_switch_to_auto(janus_home, monkeypatch):
    from janus import cli
    state = permissions.ModeState(current="default")
    monkeypatch.setattr(cli, "_RUN_STATE", {"mode_state": state})
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_mode("auto")
    output = buf.getvalue()
    assert state.current == permissions.AUTO
    assert "mode -> auto" in output
    # Should mention how auto behaves
    assert "blocked" in output.lower()


def test_cli_basic_mode_switch_legacy_auto_string(janus_home, monkeypatch):
    """Legacy `JANUS_APPROVAL=auto` users get the new auto mode (was bypass)."""
    from janus import cli
    state = permissions.ModeState(current="default")
    monkeypatch.setattr(cli, "_RUN_STATE", {"mode_state": state})
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_mode("auto")
    assert state.current == permissions.AUTO


# ---------- BUILTIN_COMMANDS docstring ----------


def test_cli_rich_mode_command_lists_auto():
    from janus import cli_rich
    mode_cmd = next(
        c for c in cli_rich.BUILTIN_COMMANDS if c.name == "/mode"
    )
    assert "auto" in mode_cmd.description
