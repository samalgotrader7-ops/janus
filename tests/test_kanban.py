"""v1.42.0 — kanban store + state machine + dispatcher smoke tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from janus.kanban import store, state, dispatcher


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "kanban.db"


def test_create_leaf_task_is_ready(db):
    t = store.create_task(
        title="leaf", agent_profile="developer", db_path=db,
    )
    assert t.status == state.READY
    assert t.parent_ids == []


def test_create_with_parent_is_backlog(db):
    p = store.create_task(title="parent", agent_profile="developer", db_path=db)
    c = store.create_task(
        title="child", agent_profile="coder",
        parent_ids=[p.id], db_path=db,
    )
    assert c.status == state.BACKLOG


def test_completing_parent_advances_child(db):
    p = store.create_task(title="parent", agent_profile="developer", db_path=db)
    c = store.create_task(
        title="child", agent_profile="coder",
        parent_ids=[p.id], db_path=db,
    )
    store.set_status(p.id, state.IN_PROGRESS, worker_id="w1", db_path=db)
    store.set_status(p.id, state.COMPLETED, output="ok", db_path=db)
    c2 = store.get_task(c.id, db_path=db)
    assert c2.status == state.READY


def test_claim_ready_is_atomic(db):
    # Two tasks ready. Two claims pick different tasks.
    t1 = store.create_task(title="a", agent_profile="developer", db_path=db)
    t2 = store.create_task(title="b", agent_profile="developer", db_path=db)
    claimed1 = store.claim_ready(worker_id="w1", db_path=db)
    claimed2 = store.claim_ready(worker_id="w2", db_path=db)
    claimed3 = store.claim_ready(worker_id="w3", db_path=db)
    ids = {claimed1.id, claimed2.id}
    assert ids == {t1.id, t2.id}
    assert claimed3 is None


def test_illegal_transition_rejected(db):
    t = store.create_task(title="x", agent_profile="developer", db_path=db)
    with pytest.raises(ValueError):
        # READY → COMPLETED is not a legal direct transition.
        store.set_status(t.id, state.COMPLETED, db_path=db)


def test_multi_parent_advance_only_when_all_done(db):
    p1 = store.create_task(title="p1", agent_profile="developer", db_path=db)
    p2 = store.create_task(title="p2", agent_profile="developer", db_path=db)
    c = store.create_task(
        title="c", agent_profile="coder",
        parent_ids=[p1.id, p2.id], db_path=db,
    )
    store.set_status(p1.id, state.IN_PROGRESS, db_path=db)
    store.set_status(p1.id, state.COMPLETED, db_path=db)
    assert store.get_task(c.id, db_path=db).status == state.BACKLOG
    store.set_status(p2.id, state.IN_PROGRESS, db_path=db)
    store.set_status(p2.id, state.COMPLETED, db_path=db)
    assert store.get_task(c.id, db_path=db).status == state.READY


def test_delete_cascades_dependencies(db):
    p = store.create_task(title="p", agent_profile="developer", db_path=db)
    c = store.create_task(
        title="c", agent_profile="coder", parent_ids=[p.id], db_path=db,
    )
    assert store.delete_task(p.id, db_path=db) is True
    # The child's dependency row should be gone; we still have the row.
    assert store.get_task(c.id, db_path=db).parent_ids == []


def test_dispatcher_runs_a_task(db, monkeypatch):
    """End-to-end with a stubbed agents.dispatch — proves the loop
    claims, executes, completes, and advances dependents."""
    # Stub the agents module so we don't actually call an LLM.
    calls: list[tuple] = []

    def fake_dispatch(name, prompt, cwd=None):
        calls.append((name, prompt, cwd))
        return f"done by {name}"

    from janus import agents as _agents
    monkeypatch.setattr(_agents, "dispatch", fake_dispatch)

    # Force the dispatcher to use our test DB.
    monkeypatch.setattr(store, "_db_path", lambda: db)
    # Faster polling so the test doesn't sleep.
    monkeypatch.setattr(dispatcher, "POLL_INTERVAL_S", 0.05)

    # Create a parent + dependent child.
    p = store.create_task(
        title="root", agent_profile="developer",
        prompt="do the thing", db_path=db,
    )
    c = store.create_task(
        title="follow-up", agent_profile="coder",
        parent_ids=[p.id], db_path=db,
    )

    # Reset module-level dispatcher state in case a prior test left it.
    dispatcher.stop(timeout_s=0.1)

    dispatcher.start()
    try:
        # Give the loop a moment to process both tasks.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            t_p = store.get_task(p.id, db_path=db)
            t_c = store.get_task(c.id, db_path=db)
            if t_p.status == state.COMPLETED and t_c.status == state.COMPLETED:
                break
            time.sleep(0.05)
    finally:
        dispatcher.stop(timeout_s=1.0)

    final_p = store.get_task(p.id, db_path=db)
    final_c = store.get_task(c.id, db_path=db)
    assert final_p.status == state.COMPLETED, f"parent: {final_p.status}"
    assert final_c.status == state.COMPLETED, f"child: {final_c.status}"
    assert final_p.output == "done by developer"
    assert final_c.output == "done by coder"
    # The dispatcher called fake_dispatch exactly twice — once per task.
    assert len(calls) == 2
    assert calls[0][0] == "developer"
    assert calls[1][0] == "coder"
