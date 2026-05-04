"""Tests for janus.permissions — v1.0 Claude-Code-shaped mode matrix."""
from __future__ import annotations

import pytest

from janus import permissions as P


# ---------- decide() matrix ----------


@pytest.mark.parametrize("risk,mode,expected", [
    # default mode
    (P.RISK_READ,  P.DEFAULT, P.ALLOW),
    (P.RISK_WRITE, P.DEFAULT, P.ASK),
    (P.RISK_EXEC,  P.DEFAULT, P.ASK),
    # acceptEdits
    (P.RISK_READ,  P.ACCEPT_EDITS, P.ALLOW),
    (P.RISK_WRITE, P.ACCEPT_EDITS, P.ALLOW),
    (P.RISK_EXEC,  P.ACCEPT_EDITS, P.ASK),
    # bypassPermissions
    (P.RISK_READ,  P.BYPASS, P.ALLOW),
    (P.RISK_WRITE, P.BYPASS, P.ALLOW),
    (P.RISK_EXEC,  P.BYPASS, P.ALLOW),
    # plan
    (P.RISK_READ,  P.PLAN, P.ALLOW),
    (P.RISK_WRITE, P.PLAN, P.DENY),
    (P.RISK_EXEC,  P.PLAN, P.DENY),
])
def test_decide_matrix(risk, mode, expected):
    assert P.decide(risk, mode) == expected


def test_decide_unknown_risk_treated_as_exec():
    # Fail closed.
    assert P.decide("bogus", P.DEFAULT) == P.ASK
    assert P.decide("bogus", P.PLAN) == P.DENY
    assert P.decide("bogus", P.BYPASS) == P.ALLOW


def test_decide_unknown_mode_treated_as_default():
    assert P.decide(P.RISK_READ,  "bogus") == P.ALLOW
    assert P.decide(P.RISK_WRITE, "bogus") == P.ASK
    assert P.decide(P.RISK_EXEC,  "bogus") == P.ASK


# ---------- normalize() ----------


def test_normalize_passthrough_for_known_modes():
    for m in P.ALL_MODES:
        assert P.normalize(m) == m


def test_normalize_legacy_names():
    assert P.normalize("manual") == P.DEFAULT
    # v1.5: "auto" is now a real mode (smart-approve). The legacy
    # `auto → bypassPermissions` mapping was removed because the new
    # auto mode is strictly safer (it analyzes args before allowing).
    assert P.normalize("auto") == P.AUTO
    assert P.normalize("dry-run") == P.PLAN


def test_normalize_unknown_falls_back_to_default():
    assert P.normalize("nonsense") == P.DEFAULT
    assert P.normalize("") == P.DEFAULT
    assert P.normalize(None) == P.DEFAULT


# ---------- cycle_next() ----------


def test_cycle_next_walks_through_all_modes():
    seen = [P.DEFAULT]
    cur = P.DEFAULT
    for _ in range(len(P.CYCLE_ORDER)):
        cur = P.cycle_next(cur)
        seen.append(cur)
    # After N cycles we wrap back to start.
    assert seen[-1] == P.DEFAULT
    assert set(seen) == set(P.CYCLE_ORDER)


def test_cycle_next_handles_unknown_input():
    assert P.cycle_next("bogus") in P.CYCLE_ORDER


# ---------- risk_from_verb() ----------


@pytest.mark.parametrize("verb,expected", [
    ("read",   P.RISK_READ),
    ("list",   P.RISK_READ),
    ("search", P.RISK_READ),
    ("fetch",  P.RISK_READ),
    ("write",  P.RISK_WRITE),
    ("edit",   P.RISK_WRITE),
    ("create", P.RISK_WRITE),
    ("exec",   P.RISK_EXEC),
    ("run",    P.RISK_EXEC),
    ("navigate", P.RISK_EXEC),
])
def test_risk_from_verb(verb, expected):
    assert P.risk_from_verb(verb) == expected


def test_risk_from_verb_unknown_fails_closed():
    assert P.risk_from_verb("frobnicate") == P.RISK_EXEC
    assert P.risk_from_verb("") == P.RISK_EXEC


# ---------- ModeState container ----------


def test_mode_state_default():
    s = P.ModeState()
    assert s.current == P.DEFAULT


def test_mode_state_set_normalizes():
    s = P.ModeState()
    # v1.5: "auto" normalizes to the real AUTO mode now (was legacy → BYPASS).
    assert s.set("auto") == P.AUTO
    assert s.current == P.AUTO


def test_mode_state_cycle():
    s = P.ModeState(current=P.DEFAULT)
    s.cycle()
    assert s.current != P.DEFAULT
    assert s.current in P.CYCLE_ORDER
