"""Tests for the `--logo` subcommand."""
from __future__ import annotations
import subprocess
import sys

import pytest

from janus import branding


def _run_janus(*args, env_extra=None):
    """Invoke `python -m janus <args>` as a subprocess and return stdout."""
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("JANUS_API_KEY", "test")  # so config.assert_configured passes
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", "janus", *args],
        capture_output=True, text=True, env=env, encoding="utf-8",
    )
    return proc


def test_logo_default_includes_logo_and_tagline():
    proc = _run_janus("--logo")
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    out = proc.stdout
    # All three logo lines present.
    for line in branding.LOGO_LINES:
        assert line in out
    # Tagline reaches stdout.
    assert branding.TAGLINE in out
    assert f"v{branding.VERSION}" in out


def test_logo_plain_is_three_lines_no_titles():
    proc = _run_janus("--logo", "--plain")
    assert proc.returncode == 0
    lines = [l for l in proc.stdout.splitlines() if l]
    assert len(lines) == 3
    # No version, no tagline.
    assert "v" not in proc.stdout or branding.VERSION not in proc.stdout
    assert branding.TAGLINE not in proc.stdout
    for expected, actual in zip(branding.LOGO_LINES, lines):
        assert expected == actual


def test_logo_svg_outputs_valid_svg():
    proc = _run_janus("--logo", "--svg")
    assert proc.returncode == 0
    out = proc.stdout.strip()
    assert out.startswith("<svg")
    assert out.endswith("</svg>")
    assert branding.BRAND_COLOR in out
