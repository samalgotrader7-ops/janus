"""Tests for v1.31.11 — Telegram concurrent_updates(True).

FIELD-VALIDATION FINDING (Sam, 2026-05-08, after v1.31.10 ship):

After v1.31.10 added text-reply fallback for plan-review
approvals, Sam tested. Sent the cost.py prompt → plan rendered
with the "💬 OR reply Y / N" hint. Sam typed "N". Bot did NOT
process the N reply. Log showed:

  on_text FIRED chat=... text='I want to add a new env var ...'
  (no entries after that)

The N reply queued behind the still-running on_text for the
cost.py prompt. PTB v20+ default is to serialize updates per-
chat (preserves message order). The cost.py on_text is blocked
in `await asyncio.to_thread(_chat_with_origin)` waiting for
the approver to return — which it can't, because the approver
is waiting on a future that the N reply was supposed to set.

Deadlock by design.

THE FIX:
``Application.builder().concurrent_updates(True)`` — allows
multiple updates from the same chat to dispatch concurrently.
The N reply's on_text now runs immediately, resolves the
pending_plan_review future, and the original cost.py on_text
unblocks.

DESIGN INVARIANT PINNED:
  * Application is built with concurrent_updates(True). If this
    regresses, the v1.31.10 text-reply fallback silently breaks
    (no test caught it because tests don't exercise the actual
    PTB dispatch model — they pin source).
"""

from __future__ import annotations

import inspect

from janus.gateways import telegram as tg_mod


def test_application_built_with_concurrent_updates():
    """Source pin: serve() builds the Application with
    concurrent_updates(True). Without this, the v1.31.10
    text-reply fallback deadlocks against the PTB serial-per-chat
    dispatch."""
    src = inspect.getsource(tg_mod.serve)
    assert "concurrent_updates(True)" in src


def test_v1_31_11_marker_in_serve():
    src = inspect.getsource(tg_mod.serve)
    assert "v1.31.11" in src


def test_concurrent_updates_appears_before_build():
    """Builder pattern: concurrent_updates must be called BEFORE
    .build(). Otherwise the setting doesn't take effect."""
    src = inspect.getsource(tg_mod.serve)
    cu_idx = src.find("concurrent_updates(True)")
    build_idx = src.find(".build()")
    assert cu_idx != -1
    assert build_idx != -1
    assert cu_idx < build_idx


def test_comment_explains_why_concurrent_updates():
    """Marker comment preserves the field-report context so
    future maintainers don't accidentally remove the setting
    thinking 'sequential is safer'."""
    src = inspect.getsource(tg_mod.serve)
    cu_idx = src.find("concurrent_updates(True)")
    region_before = src[max(0, cu_idx - 1500):cu_idx]
    # Expect at least one mention of "deadlock", "text-reply",
    # or "serialize" so the intent is clear.
    assert (
        "deadlock" in region_before.lower()
        or "serialize" in region_before.lower()
        or "text-reply" in region_before.lower()
        or "text reply" in region_before.lower()
    )
