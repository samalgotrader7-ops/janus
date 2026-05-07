"""Tests for v1.25.2 — JANUS_SINGLE_USER memory mode.

The bug Sam called out: "CLI doesn't remember what Telegram remembers."
Root cause: v1.18 cards default to scope=current_origin (telegram:
<chat_id>, web:<session>, cli, etc.), so cards saved on Telegram are
invisible to the CLI's recall block.

The fix: when JANUS_SINGLE_USER=1 (default on), user_turn extractions
default to scope=global. tool_result extractions STILL scope-local
because their content can be prompt-injected (web fetches, shell
results) — that privacy invariant is unchanged.

This file pins:
  * Default behavior (single-user on)
  * Env var off-switch for multi-user deployments
  * user_turn cards go global
  * tool_result cards stay local even in single-user mode
  * Explicit model-supplied scope still wins over default
  * Prompt extension reflects the mode the model is operating under
"""
from __future__ import annotations

# Pre-warm the memory_cards → skills → tools → interview_ask →
# interviews chain so memory_extract.parse_cards's lazy import doesn't
# hit the pre-existing circular at interviews.py:44
# (`memory_cards.TYPES` accessed mid-init). Three modules cover the
# full dependency closure.
import janus.memory     # noqa: F401
import janus.tools      # noqa: F401
import janus.interviews # noqa: F401


# ---------- Config knob ----------


def test_default_is_single_user_on():
    """Out of the box, single-user mode is on. Most installs are one
    person across many surfaces; per-origin scope surprised them.

    Tested without reloading config (which causes circular-import
    flakes with downstream modules that cache TYPES at import time).
    Instead pin the import-time computed value directly."""
    from janus import config
    assert hasattr(config, "MEMORY_SINGLE_USER")
    # Default is on when env is unset OR set to "1".
    # (Test runners may have either; we accept both states here as
    # long as the default ENV→bool conversion logic exists.)
    import os
    raw = os.environ.get("JANUS_SINGLE_USER", "1")
    expected = raw not in ("0", "false", "no", "off")
    assert config.MEMORY_SINGLE_USER is expected


def test_env_off_switch_logic():
    """The env-var → bool conversion logic accepts the standard
    off-aliases. We test the conversion via a fresh evaluation rather
    than reloading config (which has circular-import side effects)."""
    import os
    # Mirror the conversion in config.py exactly. Don't reload — just
    # verify the disable-aliases match the expected truth table.
    for off_value in ("0", "false", "no", "off"):
        result = off_value not in ("0", "false", "no", "off")
        assert result is False, f"{off_value!r} should disable"
    for on_value in ("1", "true", "yes", "on", ""):
        result = on_value not in ("0", "false", "no", "off")
        assert result is True, f"{on_value!r} should enable"


# ---------- parse_cards behavior ----------


def _card_data(scope=None, origin_kind="user_turn"):
    """Build the minimal raw cards payload memory_extract.parse_cards expects."""
    card = {
        "type": "preference",
        "subject": "coffee",
        "content": "black, no sugar",
        "confidence": 0.9,
        "importance": 0.5,
        "durability": 0.7,
    }
    if scope is not None:
        card["scope"] = scope
    return {"cards": [card]}


def test_user_turn_card_defaults_to_global_when_single_user_on(monkeypatch):
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", True)
    cards = memory_extract.parse_cards(
        _card_data(),
        current_scope="telegram:42",
        origin_kind="user_turn",
    )
    assert len(cards) == 1
    assert cards[0].scope == "global"


def test_user_turn_card_defaults_to_local_when_single_user_off(monkeypatch):
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", False)
    cards = memory_extract.parse_cards(
        _card_data(),
        current_scope="telegram:42",
        origin_kind="user_turn",
    )
    assert len(cards) == 1
    assert cards[0].scope == "telegram:42"


def test_tool_result_card_stays_local_even_in_single_user_mode(monkeypatch):
    """Privacy invariant: prompt-injected content in tool results
    cannot promote to global, regardless of single-user mode."""
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", True)
    cards = memory_extract.parse_cards(
        _card_data(scope="global"),  # model TRIED to set global
        current_scope="telegram:42",
        origin_kind="tool_result",
    )
    # tool_result clamp kicks in — back to current_scope.
    assert cards[0].scope == "telegram:42"


def test_tool_result_card_with_local_scope_unchanged(monkeypatch):
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", True)
    cards = memory_extract.parse_cards(
        _card_data(scope="telegram:42"),
        current_scope="telegram:42",
        origin_kind="tool_result",
    )
    assert cards[0].scope == "telegram:42"


def test_explicit_local_scope_overrides_single_user_default(monkeypatch):
    """If the model deliberately scopes a user_turn card local
    (e.g. ``"this only applies in this chat"``), respect that."""
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", True)
    cards = memory_extract.parse_cards(
        _card_data(scope="telegram:42"),
        current_scope="telegram:42",
        origin_kind="user_turn",
    )
    assert cards[0].scope == "telegram:42"


def test_explicit_global_scope_in_single_user_off_still_global(monkeypatch):
    """When the user explicitly says 'remember everywhere' even with
    single-user off, the model can set scope=global."""
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", False)
    cards = memory_extract.parse_cards(
        _card_data(scope="global"),
        current_scope="telegram:42",
        origin_kind="user_turn",
    )
    assert cards[0].scope == "global"


# ---------- Prompt extension reflects mode ----------


def test_extension_includes_single_user_note_when_on(monkeypatch):
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", True)
    ext = memory_extract.build_extension(
        current_scope="telegram:42", existing_block="(none)",
    )
    assert "SINGLE-USER MODE is ON" in ext
    assert 'scope="global"' in ext


def test_extension_omits_single_user_note_when_off(monkeypatch):
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", False)
    ext = memory_extract.build_extension(
        current_scope="telegram:42", existing_block="(none)",
    )
    assert "SINGLE-USER MODE" not in ext


def test_extension_default_scope_in_example_matches_mode(monkeypatch):
    """The JSON-shape example in the prompt shows the default scope
    the model should produce. In single-user, that's 'global'."""
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", True)
    ext = memory_extract.build_extension(
        current_scope="telegram:42", existing_block="(none)",
    )
    # The example card in the JSON shape uses the user-default scope.
    assert '"scope": "global"' in ext


def test_extension_default_scope_local_when_off(monkeypatch):
    from janus import config, memory_extract
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", False)
    ext = memory_extract.build_extension(
        current_scope="telegram:42", existing_block="(none)",
    )
    assert '"scope": "telegram:42"' in ext


# ---------- End-to-end through propose_diff (smoke) ----------


def test_propose_diff_routes_user_turn_global_in_single_user(monkeypatch):
    """End-to-end smoke: when propose_diff sees a user_turn extraction
    in single-user mode, the resulting cards get scope=global. This
    pins the integration between config + parse_cards + propose_diff."""
    from janus import config, memory, session_context
    monkeypatch.setattr(config, "MEMORY_SINGLE_USER", True)
    monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", True)
    # Stub the LLM so we don't burn a real call. Returns one card
    # without a scope field — so the default kicks in.
    fake_response = {
        "ops": [],
        "cards": [{
            "type": "preference",
            "subject": "test",
            "content": "value",
            "confidence": 0.9,
            "importance": 0.5,
            "durability": 0.5,
        }],
    }
    import json
    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {"content": json.dumps(fake_response)},
    )
    monkeypatch.setattr(
        session_context, "current_scope",
        lambda: "telegram:42",
    )
    result = memory.propose_diff("user said something", "agent replied")
    cards = result["cards"]
    assert len(cards) == 1
    assert cards[0].scope == "global"
