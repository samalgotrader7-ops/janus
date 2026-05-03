"""Tests for janus/gateways/_common.py — pairing, home, soul, sessions, indicators."""

from __future__ import annotations

import datetime
import json

import pytest

from janus import config, memory
from janus.gateways import _common as gw


# ---------- Pairing ----------


def test_request_pairing_returns_8char_code(janus_home):
    code = gw.request_pairing("telegram", "12345", user_label="Sam")
    assert len(code) == 8
    assert all(c in "ABCDEFGHJKMNPQRSTUVWXYZ23456789" for c in code)


def test_request_pairing_idempotent_for_same_chat(janus_home):
    a = gw.request_pairing("telegram", "12345")
    b = gw.request_pairing("telegram", "12345")
    assert a == b  # same pending code reused


def test_request_pairing_distinct_per_chat(janus_home):
    a = gw.request_pairing("telegram", "1")
    b = gw.request_pairing("telegram", "2")
    assert a != b


def test_approve_code_moves_pending_to_approved(janus_home):
    code = gw.request_pairing("telegram", "12345", user_label="Sam")
    pc = gw.approve_code(code)
    assert pc is not None
    assert pc.gateway == "telegram"
    assert pc.chat_id == "12345"
    # No longer pending.
    assert all(p.code != code for p in gw.list_pending())
    # Now approved.
    assert "12345" in gw.list_approved().get("telegram", [])
    assert gw.is_authorized("telegram", "12345")


def test_approve_lowercase_code(janus_home):
    code = gw.request_pairing("telegram", "1")
    pc = gw.approve_code(code.lower())
    assert pc is not None


def test_approve_unknown_code_returns_none(janus_home):
    assert gw.approve_code("ZZZZZZZZ") is None


def test_is_authorized_falls_back_to_env_allowlist(janus_home):
    """Backward-compat: legacy JANUS_TELEGRAM_CHATS still works."""
    assert not gw.is_authorized("telegram", "9999")
    assert gw.is_authorized("telegram", "9999",
                            env_allowlist="9999,8888")


def test_revoke_removes_from_approved(janus_home):
    code = gw.request_pairing("telegram", "1")
    gw.approve_code(code)
    assert gw.revoke("telegram", "1") is True
    assert not gw.is_authorized("telegram", "1")
    assert gw.revoke("telegram", "1") is False  # idempotent: already gone


def test_expired_codes_are_dropped(janus_home, monkeypatch):
    """Codes >TTL old should not approve."""
    code = gw.request_pairing("telegram", "1")
    # Forcibly age the pending entry past TTL.
    pending = gw._load_pending()
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(seconds=gw._CODE_TTL_SECONDS + 60))
    pending[0].created_at = old.isoformat(timespec="seconds")
    gw._save_pending(pending)
    assert gw.approve_code(code) is None
    assert gw.list_pending() == []


# ---------- Home channel ----------


def test_home_channel_set_get_clear(janus_home):
    assert gw.get_home("telegram") is None
    gw.set_home("telegram", "12345")
    assert gw.get_home("telegram") == "12345"
    gw.set_home("whatsapp", "+15551234")
    assert gw.all_homes() == {"telegram": "12345", "whatsapp": "+15551234"}
    gw.clear_home("telegram")
    assert gw.get_home("telegram") is None
    assert gw.get_home("whatsapp") == "+15551234"


# ---------- Soul ----------


def test_agent_name_default_when_empty(janus_home):
    assert gw.agent_name() == "Janus"


def test_agent_name_from_soul_md(janus_home):
    memory.apply([
        {"op": "create_section", "category": "soul",
         "section": "Name", "text": "Samoul"},
    ])
    assert gw.agent_name() == "Samoul"


def test_user_name_from_user_md(janus_home):
    memory.apply([
        {"op": "create_section", "category": "user",
         "section": "Identity", "text": "Sam — solo dev"},
    ])
    assert gw.user_name() == "Sam"  # first token before em-dash


def test_greeting_personalized(janus_home):
    memory.apply([
        {"op": "create_section", "category": "soul",
         "section": "Name", "text": "Samoul"},
        {"op": "create_section", "category": "user",
         "section": "Name", "text": "Sam"},
    ])
    g = gw.greeting()
    assert "Sam" in g
    assert "Samoul" in g
    assert "👋" in g


def test_greeting_falls_back_to_user_label(janus_home):
    """When user.md is empty, use the platform-supplied display name."""
    g = gw.greeting(user_label="alice")
    assert "alice" in g
    assert "Janus" in g  # default agent name


# ---------- Sessions ----------


def test_session_persists_across_load(janus_home):
    sess = gw.load_session("telegram", "12345")
    assert sess.messages == []
    sess.messages.append({"role": "user", "content": "hi"})
    sess.mode = "default"
    gw.save_session(sess)

    sess2 = gw.load_session("telegram", "12345")
    assert sess2.messages == [{"role": "user", "content": "hi"}]
    assert sess2.mode == "default"


def test_session_path_sanitizes_chat_id(janus_home):
    """Phone numbers, group IDs, bad chars — all safe on disk."""
    p = gw.session_path("whatsapp", "+1 555/abc")
    assert "/" not in p.name and " " not in p.name
    assert "+" not in p.name


def test_list_sessions_per_gateway(janus_home):
    gw.save_session(gw.load_session("telegram", "1"))
    gw.save_session(gw.load_session("telegram", "2"))
    gw.save_session(gw.load_session("whatsapp", "+1"))
    assert len(gw.list_sessions("telegram")) == 2
    assert len(gw.list_sessions("whatsapp")) == 1
    assert len(gw.list_sessions()) == 3


# ---------- Indicators ----------


def test_indicator_emitter_default_is_no_op():
    """Base IndicatorEmitter.emit() doesn't raise; just no-ops."""
    e = gw.IndicatorEmitter()
    e.thinking()
    e.skill_loaded("git-pr")
    e.tool_start("shell", "git status")
    e.tool_end("shell", True, "clean")
    e.memory_update(2, "added Name to soul.md")
    e.stream_chunk("hello")
    e.done(0.0023, 1234)


def test_callback_emitter_collects_events():
    events: list[gw.Indicator] = []
    e = gw.CallbackEmitter(events.append)
    e.skill_loaded("git-pr")
    e.tool_start("shell", "git status")
    e.tool_end("shell", True)
    assert [ind.kind for ind in events] == ["skill_loaded", "tool_start", "tool_end"]
    assert events[0].payload["name"] == "git-pr"
    assert events[2].payload["success"] is True


def test_callback_emitter_swallows_exceptions():
    """Indicators are best-effort — a broken renderer mustn't crash the agent."""
    def boom(ind):
        raise RuntimeError("render failed")
    e = gw.CallbackEmitter(boom)
    e.thinking()  # must not raise
    e.tool_start("x")  # must not raise


def test_glyphs_cover_all_indicator_kinds():
    """Every documented indicator kind has a glyph (parity with Hermes UX)."""
    for kind in gw.INDICATOR_KINDS:
        assert kind in gw.INDICATOR_GLYPHS, f"missing glyph for {kind}"


# ---------- L3 #6 — per-chat soul overlay ----------


def test_soul_per_chat_overlay_extends_base(janus_home):
    memory.apply([{
        "op": "create_section", "category": "soul",
        "section": "Name", "text": "Janus",
    }])
    # Drop a per-chat overlay file.
    (config.MEMORY_DIR / "soul.99999.md").write_text(
        "# soul.99999.md\n\n## Name\nSamoul\n", encoding="utf-8",
    )
    # Default agent_name (no chat) still returns base.
    assert gw.agent_name() == "Janus"
    # With chat_id, overlay wins (the overlay's Name section appears later
    # so the lookup picks the overlaid value via section ordering).
    overlaid = gw.load_soul(chat_id="99999")
    assert "Samoul" in overlaid
    assert "Janus" in overlaid  # base preserved
    assert "(per-chat overlay)" in overlaid


def test_soul_per_chat_no_overlay_returns_base(janus_home):
    memory.apply([{
        "op": "create_section", "category": "soul",
        "section": "Name", "text": "Janus",
    }])
    # Other chat with no overlay → base only, no marker text.
    body = gw.load_soul(chat_id="00000")
    assert body.strip() == memory.read("soul").strip()
    assert "(per-chat overlay)" not in body


def test_greeting_personalizes_via_overlay(janus_home):
    memory.apply([{
        "op": "create_section", "category": "soul",
        "section": "Name", "text": "Janus",
    }])
    (config.MEMORY_DIR / "soul.42.md").write_text(
        "# soul.42.md\n\n## Name\nSamoul\n", encoding="utf-8",
    )
    # Without chat_id → "Janus".
    assert "Janus" in gw.greeting()
    # With chat_id 42 → "Samoul" (overlay's Name lookup picks the LAST one).
    msg = gw.greeting(chat_id="42")
    assert "Samoul" in msg


def test_overlay_path_safe_for_weird_chat_ids(janus_home):
    """Phone numbers and weird IDs don't blow up the soul lookup."""
    body = gw.load_soul(chat_id="+1 555/abc")  # spaces, slashes, plus
    assert isinstance(body, str)


# ---------- L3 #3 — cross-platform identity ----------


def test_link_and_lookup_identity(janus_home):
    gw.link_identity("sam", "telegram", "12345")
    gw.link_identity("sam", "whatsapp", "+15551234")
    gw.link_identity("sam", "web", "browser-abc")
    assert gw.identity_for("telegram", "12345") == "sam"
    assert gw.identity_for("whatsapp", "+15551234") == "sam"
    assert gw.identity_for("web", "browser-abc") == "sam"
    # Unlinked stranger → None.
    assert gw.identity_for("telegram", "99999") is None


def test_link_identity_idempotent(janus_home):
    gw.link_identity("sam", "telegram", "1")
    gw.link_identity("sam", "telegram", "1")  # duplicate
    pairs = gw.list_identities()["sam"]
    assert pairs.count(["telegram", "1"]) == 1


def test_unlink_identity_returns_owner(janus_home):
    gw.link_identity("sam", "telegram", "1")
    name = gw.unlink_identity("telegram", "1")
    assert name == "sam"
    assert gw.identity_for("telegram", "1") is None
    # Re-unlink → None
    assert gw.unlink_identity("telegram", "1") is None


def test_distinct_identities_isolated(janus_home):
    gw.link_identity("sam", "telegram", "1")
    gw.link_identity("alice", "telegram", "2")
    assert gw.identity_for("telegram", "1") == "sam"
    assert gw.identity_for("telegram", "2") == "alice"
    snap = gw.list_identities()
    assert set(snap.keys()) == {"sam", "alice"}
