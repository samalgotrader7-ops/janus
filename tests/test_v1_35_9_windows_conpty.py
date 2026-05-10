"""Tests for v1.35.9 — Windows ConPTY framework (Phase 8.6)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from janus.tools import shell_pty_win as spw


def test_is_windows_returns_bool():
    assert isinstance(spw.is_windows(), bool)


def test_has_pywinpty_returns_bool():
    """has_pywinpty never raises — returns False on import error."""
    assert isinstance(spw.has_pywinpty(), bool)


def test_has_pywinpty_false_on_non_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert spw.has_pywinpty() is False


def test_capability_summary_posix(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert "POSIX" in spw.capability_summary()


def test_capability_summary_windows_no_pywinpty(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(spw, "has_pywinpty", lambda: False)
    summary = spw.capability_summary()
    assert "Windows" in summary
    assert "subprocess" in summary
    assert "pip install pywinpty" in summary


def test_capability_summary_windows_with_pywinpty(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(spw, "has_pywinpty", lambda: True)
    summary = spw.capability_summary()
    assert "ConPTY" in summary or "pywinpty" in summary.lower()


def test_fallback_subprocess_args_basic():
    cmd, kwargs = spw.fallback_subprocess_args("ls", ["-la"])
    assert cmd == ["ls", "-la"]
    assert kwargs["shell"] is False


def test_fallback_subprocess_args_no_args():
    cmd, kwargs = spw.fallback_subprocess_args("ls")
    assert cmd == ["ls"]


def test_version_bumped_to_1_35_9_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 9)
