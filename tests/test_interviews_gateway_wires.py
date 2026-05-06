"""Tests for v1.19.1 gateway wires — Telegram / web / whatsapp drip mode."""

from __future__ import annotations
import datetime as _dt

import pytest

from janus import config, interviews


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    monkeypatch.setattr(
        config, "INTERVIEWS_DIR", home / "interviews", raising=False,
    )
    interviews.maybe_install_bundled()
    return home


def _q(qid: str = "q") -> interviews.Question:
    return interviews.Question(
        id=qid, question=f"Q for {qid}?", mode="text",
        importance=0.7, durability=0.7,
    )


def _lib(category: str, *qs) -> dict[str, interviews.Category]:
    return {
        category: interviews.Category(
            name=category, description="x", version=1,
            questions=list(qs),
        ),
    }


# ---------- drip_filter_category honored by get_drip_question ----------


class TestDripFilterCategory:
    def test_filter_restricts_drip_to_one_category(self, isolated_home):
        # Two categories in library; drip filter set to "preference".
        lib = {
            "identity": interviews.Category(
                name="identity", description="x", version=1,
                questions=[_q("name")],
            ),
            "preference": interviews.Category(
                name="preference", description="x", version=1,
                questions=[_q("style")],
            ),
        }
        state = interviews.InterviewState(
            gateway="telegram", chat_id="42", mode="drip",
            drip_filter_category="preference",
            drip_quota_remaining=2,
            drip_quota_resets_at=interviews._now_iso(
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=12),
            ),
        )
        interviews.save_state(state)

        result = interviews.get_drip_question(
            "telegram", "42", library=lib,
        )
        assert result is not None
        _q_text, fqid = result
        # MUST be from preference, not identity
        assert fqid.startswith("preference.")

    def test_no_filter_walks_all_categories(self, isolated_home):
        lib = {
            "identity": interviews.Category(
                name="identity", description="x", version=1,
                questions=[_q("name")],
            ),
            "preference": interviews.Category(
                name="preference", description="x", version=1,
                questions=[_q("style")],
            ),
        }
        state = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            drip_filter_category="",  # no filter
            drip_quota_remaining=2,
            drip_quota_resets_at=interviews._now_iso(
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=12),
            ),
        )
        interviews.save_state(state)

        result = interviews.get_drip_question(
            "cli", "default", library=lib,
        )
        assert result is not None
        # First eligible from SUPPORTED_CATEGORIES order — identity wins
        _q_text, fqid = result
        assert fqid.startswith("identity.")


# ---------- state persistence: drip_filter_category survives round-trip ----------


class TestStatePersistence:
    def test_filter_persisted(self, isolated_home):
        s = interviews.InterviewState(
            gateway="telegram", chat_id="123", mode="drip",
            drip_filter_category="goal",
            drip_quota_remaining=1,
        )
        interviews.save_state(s)
        loaded = interviews.load_state("telegram", "123")
        assert loaded.drip_filter_category == "goal"


# ---------- web gateway ----------


class TestWebInterviewHandle:
    def test_default_starts_drip_no_filter(self, isolated_home):
        from janus.gateways.web import _web_interview_handle
        out = _web_interview_handle("session-1", "")
        assert "interview mode on" in out
        # Default per_day was 10
        assert "10 question" in out
        # Verify state
        state = interviews.load_state("web", "session-1")
        assert state.mode == "drip"
        assert state.drip_filter_category == ""

    def test_category_arg_sets_filter(self, isolated_home):
        from janus.gateways.web import _web_interview_handle
        out = _web_interview_handle("session-1", "preference")
        assert "preference" in out
        state = interviews.load_state("web", "session-1")
        assert state.drip_filter_category == "preference"

    def test_daily_arg_sets_slow_drip(self, isolated_home):
        from janus.gateways.web import _web_interview_handle
        out = _web_interview_handle("session-1", "daily 3")
        assert "3 question" in out
        state = interviews.load_state("web", "session-1")
        assert state.drip_quota_remaining == 3
        assert state.drip_filter_category == ""

    def test_pause_stops_drip(self, isolated_home):
        from janus.gateways.web import _web_interview_handle
        # Start drip
        _web_interview_handle("session-1", "")
        # Then pause
        out = _web_interview_handle("session-1", "pause")
        assert "paused" in out.lower()
        state = interviews.load_state("web", "session-1")
        assert state.mode == "idle"
        assert state.drip_filter_category == ""

    def test_invalid_category_returns_usage(self, isolated_home):
        from janus.gateways.web import _web_interview_handle
        out = _web_interview_handle("session-1", "not_a_category")
        assert "usage" in out.lower()
        # State NOT changed to drip
        state = interviews.load_state("web", "session-1")
        assert state.mode == "idle"

    def test_about_me_renders_profile(self, isolated_home):
        from janus.gateways.web import _web_interview_handle
        out = _web_interview_handle("session-1", "about-me")
        # Empty memory → "nothing yet"
        assert "nothing yet" in out.lower() or "what i know" in out.lower()


# ---------- whatsapp gateway ----------


class TestWhatsappInterviewHandle:
    def test_default_starts_drip(self, isolated_home):
        from janus.gateways.whatsapp import _whatsapp_interview_handle
        out = _whatsapp_interview_handle("+15551234", "")
        assert "interview mode on" in out
        state = interviews.load_state("whatsapp", "+15551234")
        assert state.mode == "drip"

    def test_category_filter(self, isolated_home):
        from janus.gateways.whatsapp import _whatsapp_interview_handle
        out = _whatsapp_interview_handle("+15551234", "habit")
        state = interviews.load_state("whatsapp", "+15551234")
        assert state.drip_filter_category == "habit"

    def test_pause_stops(self, isolated_home):
        from janus.gateways.whatsapp import _whatsapp_interview_handle
        _whatsapp_interview_handle("+15551234", "")
        out = _whatsapp_interview_handle("+15551234", "pause")
        assert "paused" in out.lower()

    def test_about_me(self, isolated_home):
        from janus.gateways.whatsapp import _whatsapp_interview_handle
        out = _whatsapp_interview_handle("+15551234", "about-me")
        assert "what i know" in out.lower() or "nothing yet" in out.lower()


# ---------- per-gateway state isolation ----------


class TestStateIsolation:
    def test_telegram_state_does_not_leak_to_web(self, isolated_home):
        # Telegram chat 42 enables drip; web session must be unaffected.
        from janus.gateways.web import _web_interview_handle

        # Set up telegram state directly
        s = interviews.InterviewState(
            gateway="telegram", chat_id="42", mode="drip",
            drip_filter_category="preference",
        )
        interviews.save_state(s)

        # Web session — different gateway / chat_id — should be idle
        web_state = interviews.load_state("web", "abc")
        assert web_state.mode == "idle"
        assert web_state.drip_filter_category == ""
