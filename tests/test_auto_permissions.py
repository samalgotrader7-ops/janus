"""Tests for v1.5 'auto' permission mode + make_protected helper."""
from __future__ import annotations

import pytest

from janus import auto_mode, permissions
from janus.tools.base import make_protected
from janus.tools.capabilities import CapabilitySet


@pytest.fixture(autouse=True)
def reset_auto_patterns():
    auto_mode.reload_patterns()
    yield
    auto_mode.reload_patterns()


# ---------- permissions module ----------


def test_auto_in_all_modes():
    assert permissions.AUTO == "auto"
    assert permissions.AUTO in permissions.ALL_MODES


def test_auto_in_cycle_order():
    assert permissions.AUTO in permissions.CYCLE_ORDER


def test_auto_decision_matrix_baseline_allow():
    """Auto matrix says ALLOW for all risks. The risk analyzer wraps
    on top to flip allow→deny per individual call (not via the matrix)."""
    assert permissions.decide("read", "auto") == "allow"
    assert permissions.decide("write", "auto") == "allow"
    assert permissions.decide("exec", "auto") == "allow"


def test_normalize_accepts_auto():
    assert permissions.normalize("auto") == "auto"


def test_cycle_visits_auto():
    """Cycling through modes hits auto exactly once before wrapping."""
    seen = []
    cur = permissions.DEFAULT
    for _ in range(len(permissions.CYCLE_ORDER) + 1):
        cur = permissions.cycle_next(cur)
        seen.append(cur)
    assert "auto" in seen


# ---------- make_protected helper ----------


def test_make_protected_default_mode_is_just_capability_aware():
    """For non-auto modes, make_protected behaves exactly like
    make_capability_aware (no auto wrapping)."""
    base_calls: list = []

    def base(action, details, **kw):
        base_calls.append(kw)
        return True

    caps = CapabilitySet()
    wrapped = make_protected(base, caps, "default")
    # Dangerous shell call: should reach base (since no auto layer).
    result = wrapped(
        "shell", "rm -rf /",
        tool_name="shell.exec", args={"cmd": "rm -rf /"},
    )
    assert result is True
    assert len(base_calls) == 1


def test_make_protected_auto_mode_blocks_dangerous_call():
    """In auto mode, the wrapper blocks dangerous calls before base."""
    base_calls: list = []

    def base(action, details, **kw):
        base_calls.append(kw)
        return True

    caps = CapabilitySet()
    wrapped = make_protected(base, caps, "auto")
    result = wrapped(
        "shell", "rm -rf /",
        tool_name="shell.exec", args={"cmd": "rm -rf /"},
    )
    assert result is False
    assert base_calls == []  # Auto blocked before reaching base


def test_make_protected_auto_mode_allows_safe_call():
    base_calls: list = []

    def base(action, details, **kw):
        base_calls.append(kw)
        return True

    caps = CapabilitySet()
    wrapped = make_protected(base, caps, "auto")
    result = wrapped(
        "shell", "ls",
        tool_name="shell.exec", args={"cmd": "ls"},
    )
    assert result is True
    assert len(base_calls) == 1


def test_make_protected_capability_grant_still_short_circuits():
    """make_protected uses capability_aware as the inner layer — granted
    actions short-circuit to True before reaching the base or auto layer.

    Wait: this test would be wrong. Auto wraps OUTSIDE capability_aware,
    so auto fires FIRST. A capability-granted dangerous shell call should
    still be BLOCKED by auto. Confirmed by test_auto_aware_composes_with_
    capability_aware in test_approver_args.py.

    Here we verify the safe-but-granted path: auto allows, then cap
    short-circuits to True.
    """
    base_calls: list = []

    def base(action, details, **kw):
        base_calls.append(kw)
        return False  # would deny

    caps = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    wrapped = make_protected(base, caps, "auto")
    result = wrapped(
        "shell", "git status",
        tool_name="shell.exec", args={"cmd": "git status"},
        capability=("shell", "exec", "git status"),
    )
    assert result is True
    assert base_calls == []  # capability short-circuit


def test_make_protected_auto_blocks_even_with_capability():
    """Capability widening doesn't override auto-mode safety: a granted
    shell.exec for `rm *` still blocks `rm -rf /`."""
    base_calls: list = []

    def base(action, details, **kw):
        base_calls.append(kw)
        return True

    caps = CapabilitySet.from_dict({"shell.exec": ["rm *"]})
    wrapped = make_protected(base, caps, "auto")
    result = wrapped(
        "shell", "rm -rf /",
        tool_name="shell.exec", args={"cmd": "rm -rf /"},
        capability=("shell", "exec", "rm -rf /"),
    )
    assert result is False
    assert base_calls == []


# ---------- Integration smoke ----------


def test_auto_mode_via_decide_then_wrapper_combo():
    """Demonstrates the full v1.5 auto-mode flow:
       1. Matrix says auto/exec → allow (baseline)
       2. Wrapper analyzes args → flips to deny on bad command
       3. Without wrapper, the matrix decision would have allowed it
    """
    # Bare matrix: auto + exec = allow.
    assert permissions.decide("exec", "auto") == "allow"

    # But the wrapper catches the danger.
    base = lambda *a, **kw: True  # noqa
    wrapped = make_protected(base, CapabilitySet(), "auto")
    assert wrapped(
        "shell", "rm -rf /",
        tool_name="shell.exec", args={"cmd": "rm -rf /"},
    ) is False
