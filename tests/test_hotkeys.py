"""Tests for v1.25.5 — cli_rich keyboard hotkeys.

Each hotkey injects the matching slash command into the prompt buffer
and accepts it, so behavior is identical to the user typing the slash
by hand. This file pins:
  * The bindings exist in cli_rich (source-level)
  * `/mode cycle` advances through the canonical mode order
  * The PromptSession is constructed with key_bindings= (not orphan)
"""
from __future__ import annotations

import inspect

import pytest


# ---------- Source-level pins for the bindings ----------


def test_cli_rich_imports_keybindings():
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.key_binding import KeyBindings  # noqa: F401
    from janus import cli_rich  # noqa: F401


def test_ctrl_r_binding_present():
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    # Check that the binding is registered with the right key.
    assert '@bindings.add("c-r")' in src


def test_ctrl_z_binding_present():
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert '@bindings.add("c-z")' in src


def test_ctrl_l_binding_present():
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert '@bindings.add("c-l")' in src


def test_alt_m_mode_cycle_binding_present():
    """Ctrl+M maps to Enter on most terminals, so we use Alt+M (escape m)."""
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert '@bindings.add("escape", "m")' in src


def test_alt_p_plan_toggle_binding_present():
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert '@bindings.add("escape", "p")' in src


def test_alt_h_help_binding_present():
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert '@bindings.add("escape", "h")' in src


def test_prompt_session_uses_bindings():
    """The KeyBindings object must be passed to PromptSession; otherwise
    the handlers are dead code."""
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "key_bindings=bindings" in src


def test_inject_and_submit_helper_present():
    """The shared helper that all hotkeys use to send their slash."""
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "_inject_and_submit" in src


def test_help_overlay_lists_each_hotkey():
    """The Alt+H cheatsheet must mention every binding so users
    can discover them."""
    pytest.importorskip("prompt_toolkit")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    for needle in ("Ctrl+R", "Ctrl+Z", "Ctrl+L", "Alt+M", "Alt+P", "Alt+H"):
        assert needle in src, f"hotkey help missing {needle!r}"


# ---------- /mode cycle ----------


def _build_state(mode):
    """Minimal state dict matching what _cmd_mode expects."""
    from janus import permissions
    return {"mode_state": permissions.ModeState(current=mode)}


class _MutedConsole:
    """Console double — silences output during /mode cycle assertions."""
    def __init__(self):
        self.printed = []

    def print(self, *a, **kw):
        self.printed.append(a)


def test_mode_cycle_advances_default_to_acceptEdits():
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = _build_state(permissions.DEFAULT)
    cli_rich._cmd_mode(_MutedConsole(), "cycle", state)
    assert state["mode_state"].current == permissions.ACCEPT_EDITS


def test_mode_cycle_acceptEdits_to_plan():
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = _build_state(permissions.ACCEPT_EDITS)
    cli_rich._cmd_mode(_MutedConsole(), "cycle", state)
    assert state["mode_state"].current == permissions.PLAN


def test_mode_cycle_plan_to_auto():
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = _build_state(permissions.PLAN)
    cli_rich._cmd_mode(_MutedConsole(), "cycle", state)
    assert state["mode_state"].current == permissions.AUTO


def test_mode_cycle_auto_to_bypass():
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = _build_state(permissions.AUTO)
    cli_rich._cmd_mode(_MutedConsole(), "cycle", state)
    assert state["mode_state"].current == permissions.BYPASS


def test_mode_cycle_bypass_wraps_to_default():
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = _build_state(permissions.BYPASS)
    cli_rich._cmd_mode(_MutedConsole(), "cycle", state)
    assert state["mode_state"].current == permissions.DEFAULT


def test_mode_cycle_full_loop_returns_to_start():
    """5 cycles return to the original mode."""
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = _build_state(permissions.DEFAULT)
    for _ in range(5):
        cli_rich._cmd_mode(_MutedConsole(), "cycle", state)
    assert state["mode_state"].current == permissions.DEFAULT


def test_mode_cycle_unknown_current_falls_back_to_default():
    """If somehow current is set to something not in the canonical
    order, cycle starts at index 0 (default's slot+1 → acceptEdits).
    Defensive against state corruption."""
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = {"mode_state": permissions.ModeState(current="xxx")}
    cli_rich._cmd_mode(_MutedConsole(), "cycle", state)
    # idx becomes -1 → next is order[0] = DEFAULT
    assert state["mode_state"].current == permissions.DEFAULT
