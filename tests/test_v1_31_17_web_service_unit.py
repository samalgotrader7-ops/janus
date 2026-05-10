"""Tests for v1.31.17 — janus-web systemd unit + telegram unbuffered.

FIELD-VALIDATION FINDING (Sam, 2026-05-09 evening):

After the v1.31.5-v1.31.16 arc Sam was running three janus
processes via ``nohup`` in a tmux/SSH shell:
  - janus telegram (pid 84869)
  - janus web (pid 84322)
  - daemon (not yet started)

Two pains:
  1. None survive a reboot
  2. ``nohup janus * > log 2>&1`` block-buffers stdout, so log
     diagnostics (version banner, getUpdates trace) don't appear
     until the buffer fills (~4-8KB).

Existing fix coverage:
  - ``janus service install`` already supports janus-telegram +
    janus-daemon (systemd user units, since v1.16).
  - v1.31.15 fixed the buffer issue for web (sys.stdout.
    reconfigure + flush=True).

Gaps closed by v1.31.17:
  A. Add ``janus-web`` to the SERVICES list so users can run
     ``janus service install`` and get all three units (telegram,
     daemon, web) with one command. Bonus: systemd uses journalctl
     (line-buffered per record), which solves the buffering issue
     for free.
  B. Apply the same v1.31.15 stdout-flush fix to telegram.py for
     non-systemd nohup setups.

DESIGN INVARIANTS PINNED:
  * SERVICES list contains janus-web with exec_args=["web"]
  * janus-web has the same shape as the other entries (description,
    needs_env list)
  * The unit body for janus-web renders identically to telegram +
    daemon (Type=simple, Restart=on-failure, EnvironmentFile=-...)
  * telegram.serve() reconfigures stdout to line-buffered
  * telegram.serve() startup print uses flush=True
  * v1.31.17 marker comments present
  * Version bumped to 1.31.17
"""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import branding, services


# -------------------- Part A: janus-web service --------------------


def test_services_list_contains_janus_web():
    """v1.31.17 — janus-web is in the SERVICES list."""
    names = [s["name"] for s in services.SERVICES]
    assert "janus-web" in names


def test_services_list_still_has_telegram_and_daemon():
    """Regression guard — v1.31.17 didn't drop the existing units."""
    names = [s["name"] for s in services.SERVICES]
    assert "janus-telegram" in names
    assert "janus-daemon" in names


def test_janus_web_service_shape():
    """The web entry has the same shape as the other services."""
    web = next(s for s in services.SERVICES if s["name"] == "janus-web")
    assert web["exec_args"] == ["web"]
    assert "description" in web
    assert isinstance(web["description"], str) and web["description"]
    assert "needs_env" in web
    assert isinstance(web["needs_env"], list)


def test_janus_web_unit_body_renders_correctly():
    """render_unit() produces a valid systemd unit for the web service."""
    web = next(s for s in services.SERVICES if s["name"] == "janus-web")
    body = services.render_unit(web)
    # Standard systemd shape
    assert "[Unit]" in body
    assert "[Service]" in body
    assert "[Install]" in body
    # ExecStart includes the `web` subcommand
    assert "ExecStart=" in body
    exec_line = next(
        line for line in body.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_line.endswith(" web")
    # Restart policy matches the other services (auto-restart on failure)
    assert "Restart=on-failure" in body
    assert "RestartSec=5s" in body
    # EnvironmentFile is OPTIONAL (leading -) so missing .env doesn't
    # fail the start.
    assert "EnvironmentFile=-" in body
    # Logs go to journal — solves the v1.31.15 buffering issue
    # for free since journald handles record framing.
    assert "StandardOutput=journal" in body
    assert "StandardError=journal" in body
    # WantedBy default.target so it auto-starts on user login + linger.
    assert "WantedBy=default.target" in body


def test_v1_31_17_marker_in_services_list():
    """Source-pin: v1.31.17 comment marker on the new entry so a
    future maintainer can grep for the field-validation context."""
    src = Path(services.__file__).read_text(encoding="utf-8")
    # The marker is in the comment block right above the janus-web
    # service definition.
    web_block_idx = src.index('"name": "janus-web"')
    pre_block = src[max(0, web_block_idx - 1500): web_block_idx]
    assert "v1.31.17" in pre_block


# -------------------- Part B: telegram unbuffered --------------------


def _telegram_source() -> str:
    p = Path(branding.__file__).parent / "gateways" / "telegram.py"
    return p.read_text(encoding="utf-8")


def test_telegram_serve_reconfigures_stdout():
    """Source-pin: telegram.serve() calls sys.stdout.reconfigure(
    line_buffering=True) like web.serve() does — so nohup-redirected
    runs flush per line instead of waiting for a 4-8KB buffer."""
    src = _telegram_source()
    serve_idx = src.index("def serve(")
    # Look only at the early part of serve() (before run_polling).
    serve_block = src[serve_idx: serve_idx + 4000]
    assert "sys.stdout.reconfigure(line_buffering=True)" in serve_block


def test_telegram_serve_reconfigure_wrapped_in_try_except():
    """Same defensive pattern as web — reconfigure can raise on
    closed/exotic stdouts; try/except keeps startup resilient."""
    src = _telegram_source()
    serve_idx = src.index("def serve(")
    serve_block = src[serve_idx: serve_idx + 4000]
    reconfig_idx = serve_block.index(
        "sys.stdout.reconfigure(line_buffering=True)"
    )
    # try: should appear before reconfigure within ~200 chars
    pre = serve_block[max(0, reconfig_idx - 200): reconfig_idx]
    assert "try:" in pre
    # except: should appear after, within ~200 chars
    post = serve_block[reconfig_idx: reconfig_idx + 200]
    assert "except" in post


def test_telegram_startup_banner_uses_flush_true():
    """Belt-and-suspenders — the startup banner print uses
    flush=True so even if reconfigure failed on this stdout, the
    banner reaches the log immediately."""
    src = _telegram_source()
    banner_idx = src.index("janus telegram gateway running")
    # The print() call spans a few lines; check the next ~250 chars
    # contain flush=True.
    block = src[banner_idx: banner_idx + 250]
    assert "flush=True" in block


def test_telegram_v1_31_17_marker_present():
    """v1.31.17 comment marker present so future maintainers can
    grep for the field-validation context."""
    src = _telegram_source()
    assert "v1.31.17" in src


# -------------------- Version pin --------------------


def test_version_bumped_to_1_31_17_or_later():
    """branding.VERSION is >= 1.31.17 — that's enough; subsequent
    releases don't need to update this test."""
    def _parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

    assert _parts(branding.VERSION) >= (1, 31, 17)
