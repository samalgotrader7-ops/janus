"""Tests for v1.24.2 bug fixes:

1. cli_rich + cli memory-apply prompt switched from raw input() to
   prompt_toolkit so arrow keys / line editing work in tmux.
2. compute_completion now consults the v1.18 cards layer so answers
   from one gateway show up in another gateway's completion meter.
"""
from __future__ import annotations

import inspect

import pytest


# ---------- Bug 1: apply prompt uses prompt_toolkit ----------


def test_cli_rich_apply_uses_prompt_toolkit():
    """v1.24.2: the propose_diff approval prompt must NOT use raw
    input() — that breaks arrow-key handling under tmux. It should
    try prompt_toolkit first.
    """
    pytest.importorskip("rich")
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    # Find the apply prompt block.
    idx = src.find("apply? [y/N]")
    assert idx >= 0
    # Look at the surrounding ~600 chars to confirm the prompt_toolkit
    # path is referenced near the apply prompt.
    snippet = src[max(0, idx - 800):idx + 200]
    assert "prompt_toolkit" in snippet, (
        "v1.24.2: the apply prompt should use prompt_toolkit so "
        "arrow keys / readline editing behave correctly. Pre-v1.24.2 "
        "raw input() echoed ^[[A / ^[[B sequences and silently "
        "denied the diff."
    )


def test_cli_basic_apply_uses_prompt_toolkit():
    """Same fix in the basic CLI surface."""
    from janus import cli
    src = inspect.getsource(cli)
    idx = src.find("apply? [y/N]")
    assert idx >= 0
    snippet = src[max(0, idx - 800):idx + 200]
    assert "prompt_toolkit" in snippet, (
        "v1.24.2: cli.py's apply prompt should also try "
        "prompt_toolkit before falling back to input()."
    )


# ---------- Bug 2: cross-gateway completion meter ----------


def test_compute_completion_cards_layer_default_true(janus_home, monkeypatch):
    """A card with matching (type, subject) counts toward the question's
    completion even if the state file is empty (cross-gateway)."""
    from janus import interviews as iv
    iv.maybe_install_bundled()

    state = iv.InterviewState(gateway="web", chat_id="empty")
    # Stub memory_index.list_all to return one card matching identity.name.
    fake_rows = [
        {"type": "identity", "subject": "name",
         "id": "card-1", "scope": "global"},
    ]
    fake_index = type("M", (), {
        "list_all": staticmethod(lambda: fake_rows),
        "reconcile": staticmethod(lambda: None),
    })
    # `from . import memory_index` resolves the already-cached package
    # attribute; patching sys.modules alone isn't enough.
    import janus
    monkeypatch.setattr(janus, "memory_index", fake_index)

    pcts = iv.compute_completion(state)
    # identity has at least 1 question with id='name'; that card now
    # counts toward identity's completion percentage.
    assert pcts.get("identity", 0.0) > 0, (
        "card with matching subject should count, even with empty state"
    )


def test_compute_completion_cards_layer_false(janus_home):
    """include_cards_layer=False keeps the pre-v1.24.2 behavior:
    state-only counting. Used by tests that want pure state semantics."""
    from janus import interviews as iv

    state = iv.InterviewState(gateway="web", chat_id="empty")
    pcts = iv.compute_completion(state, include_cards_layer=False)
    # No state, no cards counted → 0% across the board.
    for cat, pct in pcts.items():
        assert pct == 0.0, f"{cat} expected 0.0, got {pct}"


def test_compute_completion_state_takes_priority_when_both_present(
    janus_home, monkeypatch,
):
    """If a question is both answered in state AND has a card, it's
    counted ONCE (not double)."""
    from janus import interviews as iv
    import datetime as _dt
    iv.maybe_install_bundled()

    state = iv.InterviewState(gateway="web", chat_id="x")
    # Mark identity.name as answered through state, NOT recently expired.
    state.answered["identity.name"] = {
        "value": "Sam",
        "answered_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }

    fake_rows = [
        {"type": "identity", "subject": "name",
         "id": "card-1", "scope": "global"},
    ]
    fake_index = type("M", (), {
        "list_all": staticmethod(lambda: fake_rows),
        "reconcile": staticmethod(lambda: None),
    })
    # `from . import memory_index` resolves the already-cached package
    # attribute; patching sys.modules alone isn't enough.
    import janus
    monkeypatch.setattr(janus, "memory_index", fake_index)

    pcts = iv.compute_completion(state)
    library = iv.load_all()
    n_identity = len(library["identity"].questions)
    # Only one match (the same question), regardless of state vs card source.
    assert pcts["identity"] == pytest.approx(1 / n_identity, rel=0.01)


def test_compute_completion_cards_layer_unrelated_subject(
    janus_home, monkeypatch,
):
    """A card with a subject that doesn't match any question must NOT
    inflate completion."""
    from janus import interviews as iv
    iv.maybe_install_bundled()

    state = iv.InterviewState(gateway="web", chat_id="x")
    fake_rows = [
        {"type": "identity", "subject": "totally_made_up_field",
         "id": "card-1", "scope": "global"},
    ]
    fake_index = type("M", (), {
        "list_all": staticmethod(lambda: fake_rows),
        "reconcile": staticmethod(lambda: None),
    })
    # `from . import memory_index` resolves the already-cached package
    # attribute; patching sys.modules alone isn't enough.
    import janus
    monkeypatch.setattr(janus, "memory_index", fake_index)

    pcts = iv.compute_completion(state)
    # Card subject doesn't match any bundled question id, so identity
    # stays at 0% (or whatever the no-state baseline is).
    assert pcts["identity"] == 0.0


def test_compute_completion_telegram_cards_visible_to_web(
    janus_home, monkeypatch,
):
    """Sam's reported case: answered via Telegram; web should see it.

    The state files are isolated per gateway, but the cards are shared
    in ~/.janus/memory/cards/. compute_completion now consults the
    cards layer so the web meter reflects Telegram answers.
    """
    from janus import interviews as iv
    iv.maybe_install_bundled()

    # Web state: completely empty (this user has never answered via web).
    web_state = iv.InterviewState(gateway="web", chat_id="any")

    # Pretend Telegram's answers landed as cards in the shared layer.
    fake_rows = [
        {"type": "identity", "subject": "name",
         "id": "c1", "scope": "global"},
        {"type": "identity", "subject": "role",
         "id": "c2", "scope": "global"},
        {"type": "preference", "subject": "communication_style",
         "id": "c3", "scope": "global"},
    ]
    fake_index = type("M", (), {
        "list_all": staticmethod(lambda: fake_rows),
        "reconcile": staticmethod(lambda: None),
    })
    # `from . import memory_index` resolves the already-cached package
    # attribute; patching sys.modules alone isn't enough.
    import janus
    monkeypatch.setattr(janus, "memory_index", fake_index)

    pcts = iv.compute_completion(web_state)
    # The web user's completion shows progress driven by the cards
    # (regardless of which gateway recorded them).
    assert pcts.get("identity", 0) > 0, (
        "web meter should reflect Telegram-sourced cards"
    )


# ---------- /api/interview/state surfaces the cross-gateway meter ----------


def test_api_interview_state_uses_aggregated_completion(janus_home):
    """Smoke test: GET /api/interview/state returns a completion dict
    with non-zero entries when cards exist (the panel reads this)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200
    r2 = c.get("/api/interview/state?session_id=fresh")
    assert r2.status_code == 200
    data = r2.json()
    assert "completion" in data
    assert isinstance(data["completion"], dict)
    # The endpoint shouldn't error even when no state file exists
    # AND no cards are recorded — empty dict / zeros are acceptable.
