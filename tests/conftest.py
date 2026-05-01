"""Shared pytest fixtures.

Workspace-mount caveat: stale .pyc files in this mount cannot be deleted
('Operation not permitted'). To force Python to invalidate them, we set the
source .py mtimes to a future timestamp -- guaranteeing they are newer than
any cached bytecode.
"""
from __future__ import annotations
import importlib
import os
import sys
import time
from pathlib import Path

import pytest


JANUS_MODULES = [
    "janus.logger",
    "janus.memory",
    "janus.index",
    "janus.skills",
    "janus.eval",
    "janus.planner",
    "janus.orchestrator",
    "janus.triggers.base",
    "janus.triggers.runtime",
    "janus.triggers",
    "janus.tools.fs",
    "janus.tools.shell",
    "janus.tools.web",
    "janus.tools.base",
    "janus.tools",
]


def pytest_configure(config):
    src_root = Path(__file__).resolve().parent.parent / "janus"
    future = time.time() + 3600
    for p in src_root.rglob("*.py"):
        try:
            os.utime(p, (future, future))
        except OSError:
            pass
    importlib.invalidate_caches()


@pytest.fixture
def janus_home(tmp_path, monkeypatch):
    home = tmp_path / "janushome"
    home.mkdir()
    monkeypatch.setenv("JANUS_HOME", str(home))
    monkeypatch.setenv("JANUS_API_KEY", "test-key")
    monkeypatch.setenv("JANUS_API_BASE", "http://localhost:1/v1")
    monkeypatch.setenv("JANUS_MODEL", "test-model")
    monkeypatch.setenv("JANUS_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()

    # Reload IN-PLACE so bindings already captured by test modules stay valid.
    import janus.config as cfg
    importlib.reload(cfg)
    for mod_name in JANUS_MODULES:
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception:
                pass
    cfg.ensure_home()
    yield Path(str(home))


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace janus.llm.chat with a queue-driven stub."""
    import janus.llm
    import janus.memory
    queue = []

    def chat(messages, tools=None, json_mode=False, temperature=0.7):
        if not queue:
            raise RuntimeError("fake_llm queue empty -- test forgot to enqueue")
        return queue.pop(0)

    monkeypatch.setattr(janus.llm, "chat", chat)
    monkeypatch.setattr(
        janus.memory, "_chat_with_model",
        lambda **kw: chat(kw["messages"], None, kw.get("json_mode")),
    )
    return queue
