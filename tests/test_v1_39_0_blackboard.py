"""Tests for v1.39.0 — internal blackboard primitive (Phase 10.3.0)."""

from __future__ import annotations

import json

import pytest

from janus import blackboard as bb


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    yield


# ---------- construction ----------


def test_run_id_required():
    with pytest.raises(ValueError):
        bb.Blackboard("")
    with pytest.raises(ValueError):
        bb.Blackboard("   ")


def test_path_under_blackboard_dir():
    b = bb.Blackboard("run-1")
    assert b.path.name == "run-1.json"
    assert b.path.parent.name == "blackboard"


def test_run_id_with_colons_normalized():
    """telegram:42 / web:abc should produce filesystem-safe filenames."""
    b = bb.Blackboard("telegram:42")
    assert ":" not in b.path.name
    b2 = bb.Blackboard("web/sess1")
    assert "/" not in b2.path.name


# ---------- basic get/set/delete ----------


def test_set_and_get():
    b = bb.Blackboard("run-1")
    b.set("status", "running")
    assert b.get("status") == "running"


def test_get_default_when_missing():
    b = bb.Blackboard("run-2")
    assert b.get("missing") is None
    assert b.get("missing", "default") == "default"


def test_set_complex_value():
    b = bb.Blackboard("run-3")
    b.set("results", [{"file": "foo.py", "ok": True}, {"file": "bar.py", "ok": False}])
    assert b.get("results")[0]["file"] == "foo.py"
    assert b.get("results")[1]["ok"] is False


def test_set_overwrites_existing():
    b = bb.Blackboard("run-4")
    b.set("k", "v1")
    b.set("k", "v2")
    assert b.get("k") == "v2"


def test_set_empty_key_rejected():
    b = bb.Blackboard("run-5")
    with pytest.raises(ValueError):
        b.set("", "v")
    with pytest.raises(ValueError):
        b.set(None, "v")  # type: ignore


def test_set_non_serializable_value_rejected():
    b = bb.Blackboard("run-6")
    with pytest.raises(ValueError):
        b.set("k", object())  # arbitrary object


def test_delete_existing_returns_true():
    b = bb.Blackboard("run-7")
    b.set("k", "v")
    assert b.delete("k") is True
    assert b.get("k") is None


def test_delete_missing_returns_false():
    b = bb.Blackboard("run-8")
    assert b.delete("never-set") is False


# ---------- keys / all / clear ----------


def test_keys_sorted():
    b = bb.Blackboard("run-9")
    b.set("zeta", 1)
    b.set("alpha", 2)
    b.set("middle", 3)
    assert b.keys() == ["alpha", "middle", "zeta"]


def test_keys_empty():
    b = bb.Blackboard("run-10")
    assert b.keys() == []


def test_all_returns_full_dict():
    b = bb.Blackboard("run-11")
    b.set("x", 1)
    b.set("y", "two")
    assert b.all() == {"x": 1, "y": "two"}


def test_clear_drops_blackboard_file():
    b = bb.Blackboard("run-12")
    b.set("x", 1)
    assert b.path.is_file()
    b.clear()
    assert not b.path.is_file()
    assert b.get("x") is None


def test_clear_idempotent():
    b = bb.Blackboard("run-13")
    b.clear()  # nothing there yet
    b.clear()  # still nothing
    assert b.path.is_file() is False


# ---------- update (atomic merge) ----------


def test_update_merges():
    b = bb.Blackboard("run-14")
    b.set("a", 1)
    b.update({"b": 2, "c": 3})
    assert b.all() == {"a": 1, "b": 2, "c": 3}


def test_update_overwrites_existing():
    b = bb.Blackboard("run-15")
    b.set("a", 1)
    b.update({"a": 99})
    assert b.get("a") == 99


def test_update_rejects_non_dict():
    b = bb.Blackboard("run-16")
    with pytest.raises(TypeError):
        b.update([("a", 1)])  # type: ignore


def test_update_validates_keys():
    b = bb.Blackboard("run-17")
    with pytest.raises(ValueError):
        b.update({"": 1})


def test_update_atomic_validates_all_values_first():
    """Pin: if ANY value is non-serializable, NOTHING gets written."""
    b = bb.Blackboard("run-18")
    b.set("safe", 1)
    with pytest.raises(ValueError):
        b.update({"new_safe": 2, "broken": object()})
    # After failed update, safe is unchanged AND new_safe was NOT
    # written
    assert b.get("safe") == 1
    assert b.get("new_safe") is None


# ---------- file durability ----------


def test_atomic_write_round_trip(tmp_path, monkeypatch):
    """Pin: state survives a fresh Blackboard instance reading the
    same file (i.e. it's actually on disk, not just in memory)."""
    b = bb.Blackboard("run-19")
    b.set("survives", True)
    b2 = bb.Blackboard("run-19")  # fresh instance
    assert b2.get("survives") is True


def test_corrupted_json_returns_empty(tmp_path, monkeypatch):
    """Pin: a malformed file should not crash — degrade to empty
    blackboard."""
    b = bb.Blackboard("run-20")
    b.path.parent.mkdir(parents=True, exist_ok=True)
    b.path.write_text("not valid json {{{", encoding="utf-8")
    assert b.all() == {}
    # And we can write fresh state on top
    b.set("x", 1)
    assert b.get("x") == 1


def test_no_temp_files_left_after_set():
    b = bb.Blackboard("run-21")
    b.set("k", "v")
    leftovers = list(b.path.parent.glob("*.tmp"))
    assert leftovers == []


# ---------- list_run_ids ----------


def test_list_run_ids_empty():
    assert bb.Blackboard.list_run_ids() == []


def test_list_run_ids_populated():
    bb.Blackboard("alice").set("x", 1)
    bb.Blackboard("bob").set("y", 2)
    bb.Blackboard("carol").set("z", 3)
    assert bb.Blackboard.list_run_ids() == ["alice", "bob", "carol"]


# ---------- module-level convenience ----------


def test_module_get_set():
    bb.set_value("run-30", "k", "v")
    assert bb.get("run-30", "k") == "v"


def test_module_delete():
    bb.set_value("run-31", "k", "v")
    assert bb.delete("run-31", "k") is True
    assert bb.delete("run-31", "k") is False


def test_module_clear():
    bb.set_value("run-32", "k", "v")
    bb.clear("run-32")
    assert bb.all_for("run-32") == {}


# ---------- isolation between runs ----------


def test_runs_isolated():
    bb.Blackboard("alice").set("k", "alice-val")
    bb.Blackboard("bob").set("k", "bob-val")
    assert bb.Blackboard("alice").get("k") == "alice-val"
    assert bb.Blackboard("bob").get("k") == "bob-val"


# ---------- version ----------


def test_version_bumped_to_1_39_0():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 39, 0)
