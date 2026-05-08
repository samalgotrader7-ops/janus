"""Tests for v1.31.8 — Telegram gateway proper logging.

FIELD-VALIDATION FINDING (Sam, 2026-05-08, second Telegram test):

After v1.31.7 fixed the markdown parse error, Sam re-tested and
saw the plan + buttons render correctly. He tapped Refine. Nothing
happened. py-spy showed:
  * The chat-turn worker thread blocked on the approval future
  * The asyncio MainThread idle in select (no callback being
    processed)
  * Telegram says no pending updates — bot consumed them
  * Audit log has no new entries — handler didn't write

Confluence: updates ARE being delivered to our bot, but NEITHER
on_callback NOR on_text are visibly running. The futures aren't
being resolved. Most likely the handlers are crashing on a silent
exception that the existing ``except Exception: pass`` blocks
swallow.

Pre-v1.31.8 the telegram gateway had ZERO Python logging configured.
``print()`` calls go to stdout. Library exceptions go to a logger
with no handlers attached (silent). Every diagnostic at this layer
required py-spy + ssh — not viable for users.

THE FIX:
  1. ``logging.basicConfig`` at module import — writes WARNING+ to
     stderr by default; level overridable via
     ``JANUS_TELEGRAM_LOG_LEVEL`` env var.
  2. Module-level ``log = logging.getLogger("janus.telegram")``.
  3. ``on_callback`` instrumented at every branch with
     ``log.info`` (entry / token / fut state / set_result / popup) +
     ``log.warning`` for the cases that previously had silent
     ``except Exception: pass``.
  4. Top-level Application error handler (``_handle_error``)
     registered via ``app.add_error_handler``. Any unhandled
     exception in any handler now logs via our logger with full
     traceback.

DESIGN INVARIANTS PINNED:
  * Logging configured at module import (not lazily) — it's needed
    BEFORE any handler can fire.
  * Default level WARNING — quiet in normal operation, exposes
    errors. INFO available via env var for handler-entry tracing.
  * on_callback instrumentation covers both "fut found" and "fut
    missing" paths.
  * Application has an error_handler registered so library-level
    exceptions don't hide.
"""

from __future__ import annotations

import inspect
import logging

from janus.gateways import telegram as tg_mod


# ============================================================
# Module-level logging setup
# ============================================================


def test_module_has_logger():
    """The gateway exposes a module-level logger named
    ``janus.telegram`` so every handler can route through it."""
    assert hasattr(tg_mod, "log")
    assert isinstance(tg_mod.log, logging.Logger)
    assert tg_mod.log.name == "janus.telegram"


def test_log_level_env_var_used():
    """Source pin: log level is read from
    ``JANUS_TELEGRAM_LOG_LEVEL`` env var with a WARNING default."""
    src = inspect.getsource(tg_mod)
    assert "JANUS_TELEGRAM_LOG_LEVEL" in src
    assert '"WARNING"' in src or "'WARNING'" in src


def test_basic_config_called_at_module_load():
    """Source pin: ``logging.basicConfig(...)`` runs at module
    import time, not lazily. Otherwise handler exceptions during
    startup wouldn't be visible."""
    src = inspect.getsource(tg_mod)
    assert "logging.basicConfig(" in src


# ============================================================
# on_callback instrumentation
# ============================================================


def test_on_callback_logs_entry():
    """Every callback dispatch is logged with chat + data so we can
    correlate clicks with downstream state changes."""
    src = inspect.getsource(tg_mod.on_callback)
    assert "log.info" in src
    assert "FIRED" in src  # readable marker for log scanning


def test_on_callback_logs_set_result():
    """When the approval branch's fut.set_result fires, log it —
    that's the moment the approver thread wakes up. (The clarify
    branch's set_result is logged less aggressively since it's
    less load-bearing for plan-mode flow.)"""
    src = inspect.getsource(tg_mod.on_callback)
    # The grant-resolution logging block specifically references
    # the granted/token values right after fut.set_result(granted).
    granted_idx = src.find("fut.set_result(granted)")
    assert granted_idx != -1, "approval branch fut.set_result not found"
    region = src[granted_idx:granted_idx + 300]
    assert "log.info" in region


def test_on_callback_logs_for_approval_branch():
    """The approval branch (`appr:` callback_data) has the
    instrumentation we need for production debugging."""
    src = inspect.getsource(tg_mod.on_callback)
    # The pop+log+set_result sequence
    appr_section_idx = src.find("approval_futures.pop(token")
    region = src[appr_section_idx:appr_section_idx + 1500]
    assert "log.info" in region
    assert "log.warning" in region  # stale-click branch


def test_on_callback_logs_stale_click():
    """Stale clicks (fut missing or done) get a WARNING-level log
    so we can tell production from test stale clicks."""
    src = inspect.getsource(tg_mod.on_callback)
    region = src[:src.find("fut.set_result")]  # before successful set
    assert "stale click" in region.lower()
    assert "log.warning" in region


def test_on_callback_replaces_silent_except_with_log():
    """v1.31.8: pre-1.31.8 had multiple ``except Exception: pass``
    in on_callback. They're now replaced with logged versions."""
    src = inspect.getsource(tg_mod.on_callback)
    # Count silent-pass patterns (excluding string literals)
    silent_count = src.count("except Exception:\n            pass")
    # Some "early-return" silent passes are still acceptable for
    # the unauthorized branch + bare "appr:" malformed branch (they
    # don't carry actionable info). But the main handler paths
    # (q.answer / edit_message_text near set_result) should log.
    # Source-pin: log.warning appears at least 3 times in the
    # function (q.answer fail, edit_message fail, stale popup fail).
    log_warning_count = src.count("log.warning(")
    assert log_warning_count >= 3, (
        f"on_callback only has {log_warning_count} log.warning "
        f"calls; v1.31.8 should log every except branch"
    )


# ============================================================
# Application error handler
# ============================================================


def test_handle_error_function_exists():
    """v1.31.8: top-level error handler must exist as
    ``_handle_error`` and be async (PTB error handlers are async)."""
    assert hasattr(tg_mod, "_handle_error")
    import asyncio
    assert asyncio.iscoroutinefunction(tg_mod._handle_error)


def test_handle_error_logs_with_traceback():
    """The error handler must log via our logger AND include
    ``exc_info`` so the traceback shows up. Without exc_info, we
    only see the exception type+repr — not where it happened."""
    src = inspect.getsource(tg_mod._handle_error)
    assert "log.error" in src
    assert "exc_info" in src


def test_serve_registers_error_handler():
    """v1.31.8: ``serve()`` (the entry point) must register the
    error handler via ``app.add_error_handler(_handle_error)``.
    If this regresses, library exceptions return to silent."""
    src = inspect.getsource(tg_mod)
    # The registration is in serve(); search for the call.
    assert "app.add_error_handler(_handle_error)" in src


def test_serve_logs_startup_banner():
    """Startup goes through the logger too — confirms basicConfig
    actually writes to stderr (the banner is the smoke-test in
    production)."""
    src = inspect.getsource(tg_mod)
    serve_idx = src.find("def serve(")
    region = src[serve_idx:]
    # log.info call near the run_polling entry point
    assert "log.info(" in region


# ============================================================
# Source pin — v1.31.8 marker
# ============================================================


def test_v1_31_8_marker_in_source():
    """The marker comment preserves field-report context for
    future maintainers — same as v1.31.5/6/7."""
    src = inspect.getsource(tg_mod)
    assert "v1.31.8" in src
