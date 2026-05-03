"""Tests for v1.3 per-chat cost ledger (cost.py)."""

from __future__ import annotations

from janus import config, cost


def test_per_chat_summary_empty(janus_home):
    """No ledger file → zero stats."""
    st = cost.per_chat_summary(gateway="telegram", chat_id="123")
    assert st.calls == 0
    assert st.usd == 0.0


def test_record_then_summary(janus_home):
    cost.record_per_chat(
        gateway="telegram", chat_id="123",
        model="openai/gpt-4o-mini",
        prompt_tokens=100, completion_tokens=50, usd=0.0123,
    )
    cost.record_per_chat(
        gateway="telegram", chat_id="123",
        model="openai/gpt-4o-mini",
        prompt_tokens=200, completion_tokens=100, usd=0.0246,
    )
    st = cost.per_chat_summary(gateway="telegram", chat_id="123")
    assert st.calls == 2
    assert st.prompt_tokens == 300
    assert st.completion_tokens == 150
    assert abs(st.usd - 0.0369) < 1e-6


def test_per_chat_isolated_per_chat_id(janus_home):
    cost.record_per_chat(
        gateway="telegram", chat_id="A",
        prompt_tokens=10, completion_tokens=5, usd=0.01,
    )
    cost.record_per_chat(
        gateway="telegram", chat_id="B",
        prompt_tokens=20, completion_tokens=10, usd=0.02,
    )
    a = cost.per_chat_summary(gateway="telegram", chat_id="A")
    b = cost.per_chat_summary(gateway="telegram", chat_id="B")
    assert a.usd == 0.01 and b.usd == 0.02


def test_per_chat_isolated_per_gateway(janus_home):
    cost.record_per_chat(gateway="telegram", chat_id="X", usd=0.05)
    cost.record_per_chat(gateway="web", chat_id="X", usd=0.10)
    tg = cost.per_chat_summary(gateway="telegram", chat_id="X")
    web = cost.per_chat_summary(gateway="web", chat_id="X")
    assert tg.usd == 0.05
    assert web.usd == 0.10


def test_per_chat_filter_by_identity(janus_home):
    cost.record_per_chat(gateway="telegram", chat_id="1", identity="sam", usd=0.10)
    cost.record_per_chat(gateway="whatsapp", chat_id="+1", identity="sam", usd=0.05)
    cost.record_per_chat(gateway="telegram", chat_id="2", identity="alice", usd=0.20)
    sam = cost.per_chat_summary(identity="sam")
    alice = cost.per_chat_summary(identity="alice")
    assert sam.calls == 2 and abs(sam.usd - 0.15) < 1e-6
    assert alice.calls == 1 and alice.usd == 0.20


def test_per_chat_since_filter(janus_home):
    """`since_iso` cutoff drops earlier entries."""
    # Manually write rows with controlled timestamps.
    p = config.HOME / "cost.jsonl"
    p.write_text(
        '{"ts": "2026-01-01T00:00:00+00:00", "gateway": "telegram", '
        '"chat_id": "1", "usd": 0.50}\n'
        '{"ts": "2026-06-01T00:00:00+00:00", "gateway": "telegram", '
        '"chat_id": "1", "usd": 0.25}\n',
        encoding="utf-8",
    )
    full = cost.per_chat_summary(gateway="telegram", chat_id="1")
    assert abs(full.usd - 0.75) < 1e-6
    recent = cost.per_chat_summary(
        gateway="telegram", chat_id="1",
        since_iso="2026-05-01T00:00:00+00:00",
    )
    assert recent.calls == 1
    assert recent.usd == 0.25


def test_record_handles_missing_optional_fields(janus_home):
    """Calling with minimal args shouldn't raise."""
    cost.record_per_chat(gateway="x", chat_id="y")
    st = cost.per_chat_summary(gateway="x", chat_id="y")
    assert st.calls == 1
    assert st.usd == 0.0


def test_record_swallows_filesystem_errors(janus_home, monkeypatch):
    """OS errors must NOT crash the agent (P8)."""
    def boom(*a, **kw):
        raise OSError("disk full")
    import builtins
    real_open = builtins.open
    def patched(p, *a, **kw):
        if str(p).endswith("cost.jsonl"):
            raise OSError("disk full")
        return real_open(p, *a, **kw)
    monkeypatch.setattr(builtins, "open", patched)
    cost.record_per_chat(gateway="x", chat_id="y", usd=0.01)
    # If we got here, no exception leaked.
