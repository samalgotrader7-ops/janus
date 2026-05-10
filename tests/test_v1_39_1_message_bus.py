"""Tests for v1.39.1 — message bus + bus_send / bus_recv tools (Phase 10.3.1)."""

from __future__ import annotations

import json
import time

import pytest

from janus import message_bus as mb
from janus.tools import bus as bus_tools, default_registry


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    yield


# ---------- Message dataclass ----------


def test_message_jsonl_round_trip():
    m = mb.Message(ts=1700000000.5, body="hello", from_agent="alice", kind="msg")
    line = m.to_jsonl()
    m2 = mb.Message.from_jsonl(line)
    assert m2.ts == 1700000000.5
    assert m2.body == "hello"
    assert m2.from_agent == "alice"
    assert m2.kind == "msg"


def test_message_jsonl_complex_body():
    m = mb.Message(
        ts=1.0,
        body={"file": "foo.py", "results": [1, 2, 3]},
        from_agent="bob",
    )
    m2 = mb.Message.from_jsonl(m.to_jsonl())
    assert m2.body == {"file": "foo.py", "results": [1, 2, 3]}


def test_message_from_jsonl_garbage_returns_none():
    assert mb.Message.from_jsonl("") is None
    assert mb.Message.from_jsonl("not json") is None
    assert mb.Message.from_jsonl("{}") is None  # missing required fields
    assert mb.Message.from_jsonl('{"ts": 1.0}') is None  # missing body


# ---------- send ----------


def test_send_creates_log_file():
    msg = mb.send("run-1", "hello")
    assert msg.body == "hello"
    assert msg.ts > 0
    assert mb.path_for("run-1").is_file()


def test_send_appends_to_log():
    mb.send("run-2", "first")
    mb.send("run-2", "second")
    msgs = mb.recv("run-2")
    assert [m.body for m in msgs] == ["first", "second"]


def test_send_validates_body_serializable():
    with pytest.raises(ValueError):
        mb.send("run-3", object())


def test_send_validates_body_not_none():
    with pytest.raises(ValueError):
        mb.send("run-4", None)


def test_send_validates_from_agent_type():
    with pytest.raises(ValueError):
        mb.send("run-5", "hi", from_agent=123)  # type: ignore


def test_send_complex_body():
    mb.send("run-6", {"key": "value", "items": [1, 2]})
    msgs = mb.recv("run-6")
    assert msgs[0].body == {"key": "value", "items": [1, 2]}


def test_send_kind_default_msg():
    msg = mb.send("run-7", "hi")
    assert msg.kind == "msg"


def test_send_custom_kind():
    msg = mb.send("run-8", "broke", kind="error")
    assert msg.kind == "error"


def test_send_run_id_normalized():
    """Pin: telegram:<id> creates a filesystem-safe filename."""
    mb.send("telegram:42", "hi")
    p = mb.path_for("telegram:42")
    assert ":" not in p.name
    assert p.is_file()


# ---------- recv ----------


def test_recv_empty_when_no_log():
    assert mb.recv("never-sent") == []


def test_recv_returns_oldest_first():
    mb.send("run-9", "first")
    time.sleep(0.001)  # ensure ordering
    mb.send("run-9", "second")
    msgs = mb.recv("run-9")
    assert msgs[0].body == "first"
    assert msgs[1].body == "second"
    assert msgs[0].ts < msgs[1].ts


def test_recv_since_filters():
    mb.send("run-10", "a")
    mid = time.time()
    time.sleep(0.01)
    mb.send("run-10", "b")
    msgs = mb.recv("run-10", since=mid)
    assert [m.body for m in msgs] == ["b"]


def test_recv_from_agent_filters():
    mb.send("run-11", "from-alice", from_agent="alice")
    mb.send("run-11", "from-bob", from_agent="bob")
    msgs = mb.recv("run-11", from_agent="alice")
    assert [m.body for m in msgs] == ["from-alice"]


def test_recv_limit_caps_to_most_recent():
    for i in range(5):
        mb.send("run-12", f"msg-{i}")
        time.sleep(0.001)
    msgs = mb.recv("run-12", limit=2)
    assert [m.body for m in msgs] == ["msg-3", "msg-4"]


def test_recv_combined_filters():
    mb.send("run-13", "alice-1", from_agent="alice")
    mb.send("run-13", "bob-1", from_agent="bob")
    mid = time.time()
    time.sleep(0.01)
    mb.send("run-13", "alice-2", from_agent="alice")
    msgs = mb.recv("run-13", since=mid, from_agent="alice")
    assert [m.body for m in msgs] == ["alice-2"]


def test_recv_skips_corrupted_lines(tmp_path, monkeypatch):
    """Pin: a malformed line in messages.jsonl shouldn't break
    other messages."""
    mb.send("run-14", "good-1")
    p = mb.path_for("run-14")
    # Append a junk line
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("not json garbage\n")
    mb.send("run-14", "good-2")
    msgs = mb.recv("run-14")
    assert [m.body for m in msgs] == ["good-1", "good-2"]


# ---------- clear / list_run_ids ----------


def test_clear_drops_log():
    mb.send("run-15", "x")
    assert mb.path_for("run-15").is_file()
    mb.clear("run-15")
    assert not mb.path_for("run-15").is_file()


def test_list_run_ids_empty():
    assert mb.list_run_ids() == []


def test_list_run_ids_populated():
    mb.send("alice", "x")
    mb.send("bob", "y")
    assert mb.list_run_ids() == ["alice", "bob"]


# ---------- BusSend tool ----------


def test_bus_send_tool_in_registry():
    assert "bus_send" in default_registry().names()
    assert "bus_recv" in default_registry().names()


def test_bus_send_tool_appends_message():
    out = bus_tools.BusSend().run(
        {"run_id": "run-20", "body": "hello", "from_agent": "alice"},
        lambda *a, **kw: True,
    )
    assert "ok" in out.lower()
    msgs = mb.recv("run-20")
    assert msgs[0].body == "hello"
    assert msgs[0].from_agent == "alice"


def test_bus_send_tool_requires_run_id():
    out = bus_tools.BusSend().run(
        {"run_id": "", "body": "x"},
        lambda *a, **kw: True,
    )
    assert "run_id" in out.lower()


def test_bus_send_tool_requires_body():
    out = bus_tools.BusSend().run(
        {"run_id": "run-21"},
        lambda *a, **kw: True,
    )
    assert "body" in out.lower()


def test_bus_send_tool_approver_refusal():
    out = bus_tools.BusSend().run(
        {"run_id": "run-22", "body": "x"},
        lambda *a, **kw: False,
    )
    assert "refused" in out.lower()


def test_bus_send_tool_capability_token():
    seen = {}

    def app(action, details, **kw):
        seen["cap"] = kw.get("capability")
        return False

    bus_tools.BusSend().run({"run_id": "run-23", "body": "x"}, app)
    assert seen["cap"] == ("bus", "send", "run-23")


def test_bus_send_tool_handles_non_serializable():
    out = bus_tools.BusSend().run(
        {"run_id": "run-24", "body": object()},
        lambda *a, **kw: True,
    )
    # Wrapped in tool error message
    assert "ValueError" in out or "not JSON" in out.lower()


# ---------- BusRecv tool ----------


def test_bus_recv_tool_returns_json_array():
    mb.send("run-30", "msg-1", from_agent="alice")
    mb.send("run-30", "msg-2", from_agent="bob")
    out = bus_tools.BusRecv().run(
        {"run_id": "run-30"}, lambda *a, **kw: True,
    )
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["body"] == "msg-1"
    assert parsed[1]["from_agent"] == "bob"


def test_bus_recv_tool_no_messages():
    out = bus_tools.BusRecv().run(
        {"run_id": "empty-run"}, lambda *a, **kw: True,
    )
    assert "no messages" in out.lower()


def test_bus_recv_tool_requires_run_id():
    out = bus_tools.BusRecv().run({}, lambda *a, **kw: True)
    assert "run_id" in out.lower()


def test_bus_recv_tool_filters_with_from_agent():
    mb.send("run-31", "x", from_agent="alice")
    mb.send("run-31", "y", from_agent="bob")
    out = bus_tools.BusRecv().run(
        {"run_id": "run-31", "from_agent": "alice"},
        lambda *a, **kw: True,
    )
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["body"] == "x"


def test_bus_recv_tool_limit():
    for i in range(5):
        mb.send("run-32", f"msg-{i}")
        time.sleep(0.001)
    out = bus_tools.BusRecv().run(
        {"run_id": "run-32", "limit": 2},
        lambda *a, **kw: True,
    )
    parsed = json.loads(out)
    assert len(parsed) == 2
    assert parsed[-1]["body"] == "msg-4"


def test_bus_recv_tool_garbage_filters_ignored():
    """Pin: bad `since` / `limit` types shouldn't crash — just
    treated as None."""
    mb.send("run-33", "x")
    out = bus_tools.BusRecv().run(
        {"run_id": "run-33", "since": "not-a-number", "limit": "abc"},
        lambda *a, **kw: True,
    )
    parsed = json.loads(out)
    assert parsed[0]["body"] == "x"


def test_bus_recv_tool_is_read_risk():
    assert bus_tools.BusRecv().risk == "read"
    assert bus_tools.BusRecv().dangerous is False


# ---------- version ----------


def test_version_bumped_to_1_39_1():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 39, 1)
