"""Tests for v1.31.16 — banner shows configured-vs-connected MCP count
+ telegram emitter is quiet by default (Hermes-style).

FIELD-VALIDATION FINDING (Sam, 2026-05-09 evening):

Two separate UX gaps in the same session:

1. CLI banner read "0 mcp" despite ~/.janus/mcp/servers.json having
   12 valid servers. Sam interpreted that as "MCP integration
   broken". Actually the banner counts ACTIVE (live) clients —
   nothing is connected until /mcp connect is run. Distinguishable
   from a real outage only if the banner shows BOTH numbers.

2. Telegram gateway emitted a separate message for every
   tool_start / tool_end / skill_loaded / memory_update / thinking
   event during a turn. Hermes (which Sam compared against) emits
   nothing during the turn — only the model's final summary
   message lands. Sam wants Janus telegram to behave the same way.

THE FIX:

Part A — banner format:
  - branding.BannerInputs gains an optional ``mcp_configured`` field
    (default 0 keeps existing tests/headless callers working).
  - New _format_mcp_count(connected, configured) helper:
      "0 mcp"     when configured <= 0 OR connected == configured
      "0/12 mcp"  when 12 are configured but 0 connected
      "3/12 mcp"  partial connection
  - cli_rich + cli pass mcp_configured=len(mcp_client.load_servers())

Part B — telegram quiet mode:
  - New JANUS_TELEGRAM_VERBOSE env var (default OFF, opt-in).
  - TelegramEmitter.emit() early-returns when not verbose, so the
    intermediate glyph stream stops. The model's final assistant
    message still lands, and approval / clarify / plan-review
    prompts STILL fire (they go through different code paths,
    not this emitter).
  - Existing users who depended on the verbose stream can set
    JANUS_TELEGRAM_VERBOSE=1 to bring it back.

DESIGN INVARIANTS PINNED:
  * BannerInputs.mcp_configured exists with default 0 (back-compat)
  * _format_mcp_count handles three cases (none / partial / full)
  * status_lines uses the formatter (no inline string repeat)
  * cli_rich + cli pass mcp_configured to BannerInputs
  * config.TELEGRAM_VERBOSE defaults False
  * TelegramEmitter.emit early-returns when not verbose
  * env var name is JANUS_TELEGRAM_VERBOSE
  * version bumped to 1.31.16
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from janus import branding


# -------------------- Part A: banner _format_mcp_count --------------------


def test_format_mcp_count_zero_configured_zero_connected():
    """Nothing configured, nothing connected — single number, no slash."""
    assert branding._format_mcp_count(0, 0) == "0 mcp"


def test_format_mcp_count_some_configured_none_connected():
    """The exact field shape from Sam's VPS — 12 configured, 0 connected."""
    assert branding._format_mcp_count(0, 12) == "0/12 mcp"


def test_format_mcp_count_partial_connection():
    """3 connected out of 12 configured."""
    assert branding._format_mcp_count(3, 12) == "3/12 mcp"


def test_format_mcp_count_all_connected():
    """When all configured are connected, just show the single number."""
    assert branding._format_mcp_count(12, 12) == "12 mcp"


def test_format_mcp_count_negative_configured_treated_as_zero():
    """Defensive — if a caller passes -1 or similar, fall back to single."""
    assert branding._format_mcp_count(0, -1) == "0 mcp"


def test_banner_inputs_has_mcp_configured_field_with_default():
    """BannerInputs.mcp_configured exists and defaults to 0 for
    back-compat with callers that pre-date v1.31.16."""
    b = branding.BannerInputs(
        model="m", cwd="c", home="h",
        tool_count=1, skill_count=2, mcp_count=0,
    )
    assert b.mcp_configured == 0


def test_status_lines_renders_x_over_y_when_configured_set():
    """The full status line includes "X/Y mcp" when configured > 0."""
    b = branding.BannerInputs(
        model="m", cwd="c", home="h",
        tool_count=43, skill_count=58, mcp_count=0,
        mcp_configured=12,
    )
    lines = branding.status_lines(b)
    blob = "\n".join(lines)
    assert "0/12 mcp" in blob
    assert "43 tools" in blob
    assert "58 skills" in blob


def test_status_lines_old_format_when_configured_zero():
    """When the user has nothing configured, the line stays single-number."""
    b = branding.BannerInputs(
        model="m", cwd="c", home="h",
        tool_count=43, skill_count=58, mcp_count=0,
        mcp_configured=0,
    )
    blob = "\n".join(branding.status_lines(b))
    assert "0 mcp" in blob
    assert "/" not in blob.split("0 mcp")[0].split("·")[-1]


# -------------------- Source pins (callers populate the new field) --------------------


def test_cli_rich_passes_mcp_configured():
    """cli_rich enumerates load_servers() and threads the count
    through to BannerInputs."""
    src = (
        Path(branding.__file__).parent / "cli_rich.py"
    ).read_text(encoding="utf-8")
    # Computes the configured count
    assert "mcp_configured = len(mcp_client.load_servers())" in src
    # Passes it into BannerInputs
    assert "mcp_configured=mcp_configured" in src
    # v1.31.16 marker present
    assert "v1.31.16" in src


def test_cli_basic_passes_mcp_configured():
    """The legacy cli.py also threads the new field for parity."""
    src = (
        Path(branding.__file__).parent / "cli.py"
    ).read_text(encoding="utf-8")
    assert "mcp_configured = len(mcp_client.load_servers())" in src
    assert "mcp_configured=mcp_configured" in src


# -------------------- Part B: telegram quiet by default --------------------


def test_config_has_telegram_verbose_default_false(monkeypatch):
    """JANUS_TELEGRAM_VERBOSE is OFF by default."""
    monkeypatch.delenv("JANUS_TELEGRAM_VERBOSE", raising=False)
    # Re-import config to pick up the cleared env var.
    from janus import config
    importlib.reload(config)
    assert config.TELEGRAM_VERBOSE is False


def test_config_telegram_verbose_picks_up_env(monkeypatch):
    """Setting JANUS_TELEGRAM_VERBOSE=1 enables the verbose stream."""
    monkeypatch.setenv("JANUS_TELEGRAM_VERBOSE", "1")
    from janus import config
    importlib.reload(config)
    assert config.TELEGRAM_VERBOSE is True


def test_config_telegram_verbose_accepts_truthy_strings(monkeypatch):
    """Common truthy strings all enable verbose mode."""
    for val in ("1", "true", "True", "TRUE", "yes", "on", "On"):
        monkeypatch.setenv("JANUS_TELEGRAM_VERBOSE", val)
        from janus import config
        importlib.reload(config)
        assert config.TELEGRAM_VERBOSE is True, f"failed for {val!r}"


def test_telegram_emitter_silent_by_default(monkeypatch):
    """Without the verbose env var, TelegramEmitter.emit returns
    immediately without queueing any send. The model's final
    assistant message still lands via a different path."""
    monkeypatch.delenv("JANUS_TELEGRAM_VERBOSE", raising=False)
    from janus import config
    importlib.reload(config)
    # Need to re-import the telegram module so it picks up the
    # reloaded config (since it imports config at module top).
    from janus.gateways import telegram as tg
    importlib.reload(tg)

    # Build an emitter with stub app/loop so we can verify NO send
    # was attempted.
    sent: list[str] = []

    class StubBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs.get("text", ""))

    class StubApp:
        bot = StubBot()

    class StubLoop:
        def __init__(self):
            self.scheduled = []

        # asyncio.run_coroutine_threadsafe(coro, loop) calls
        # loop.call_soon_threadsafe; we don't need real scheduling.
        def call_soon_threadsafe(self, *a, **kw):
            self.scheduled.append((a, kw))

    emitter = tg.TelegramEmitter(123, StubApp(), StubLoop())
    # Emit a tool_start indicator — should be silently dropped.
    from janus.gateways import _common as gw
    ind = gw.Indicator(
        kind="tool_start",
        payload={"name": "fs_grep", "args": "pattern=foo"},
    )
    emitter.emit(ind)

    assert sent == [], (
        f"emitter sent {sent!r} despite JANUS_TELEGRAM_VERBOSE being unset"
    )


def test_telegram_emitter_active_when_verbose_set(monkeypatch):
    """With JANUS_TELEGRAM_VERBOSE=1, the emitter resumes its
    pre-v1.31.16 behavior of sending a glyph message per event."""
    monkeypatch.setenv("JANUS_TELEGRAM_VERBOSE", "1")
    from janus import config
    importlib.reload(config)
    from janus.gateways import telegram as tg
    importlib.reload(tg)

    captured_sends: list[str] = []

    class FakeEmitter(tg.TelegramEmitter):
        def _send(self, text):
            captured_sends.append(text)

    e = FakeEmitter(123, app=None, loop=None)
    from janus.gateways import _common as gw
    e.emit(gw.Indicator(
        kind="tool_start",
        payload={"name": "fs_grep", "args": "pattern=foo"},
    ))
    e.emit(gw.Indicator(
        kind="tool_end",
        payload={"name": "fs_grep", "success": True},
    ))
    assert any("fs_grep" in s for s in captured_sends)
    assert any(s.startswith("✓") or s.startswith("✗") for s in captured_sends)


def test_telegram_emitter_source_pin_for_quiet_gate():
    """Source-pin: the early-return check is present in emit()."""
    src = (
        Path(branding.__file__).parent / "gateways" / "telegram.py"
    ).read_text(encoding="utf-8")
    emit_idx = src.index("def emit(self, ind:")
    block = src[emit_idx: emit_idx + 1500]
    assert "config.TELEGRAM_VERBOSE" in block
    # v1.31.16 marker so future maintainers can grep for context.
    assert "v1.31.16" in block


# -------------------- Version pin --------------------


def test_version_bumped_to_1_31_16_or_later():
    from janus import branding as b
    parts = tuple(int(x) for x in b.VERSION.split("."))
    assert parts >= (1, 31, 16)
    pyproject_path = (
        Path(b.__file__).parent.parent / "pyproject.toml"
    )
    py_src = pyproject_path.read_text(encoding="utf-8")
    assert 'version = "1.31.16"' in py_src
