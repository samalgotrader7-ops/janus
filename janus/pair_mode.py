"""
pair_mode.py — read-only file-watcher framework (v1.35.8, Phase 7.4).

WHY:
"Pair mode" is the model-agnostic equivalent of Cursor's tab-
complete: watch a file the user is editing, run debounced
analysis, surface suggestions inline. Pre-v1.35.8 Janus had no
file-watching primitive. This module ships the watcher
abstraction; per-platform native watching (inotify on Linux,
FSEvents on macOS) and editor integration (LSP / VS Code
extension) are future v1.35.x lifts.

DESIGN — POLLING-BASED:
Polling at 500ms is enough for human-edit-speed updates without
needing inotify/watchdog dependencies. Adapter point for native
watching is `make_watcher(path, callback)` — a future PR can
return a `WatchdogWatcher` instead of `PollingWatcher` when
watchdog is installed.

CONTRACT:
  watcher = PollingWatcher(path, on_change, interval=0.5)
  watcher.start()  # spawns a daemon thread
  ... edit the file ...
  # on_change(path, content) fires after each save (debounced)
  watcher.stop()

The on_change callback receives the FULL file content, not a diff.
Janus's role is to pass that content to the model with a prompt
like "what's wrong here?" — the model writes natural-language
suggestions, never edits.

P5 (no auto-edit): pair mode is READ-ONLY. The model NEVER
writes to the watched file. Suggestions surface in chat; the
human applies them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class PollingWatcher:
    """File watcher using polling — simplest possible implementation,
    no extra deps. Detects changes via mtime comparison."""

    path: Path
    on_change: Callable[[Path, str], None]
    interval: float = 0.5
    debounce: float = 0.3
    _thread: threading.Thread | None = None
    _stop: threading.Event | None = None
    _last_mtime: float = -1.0
    _last_change_at: float = 0.0

    def start(self) -> None:
        """Spawn a daemon thread that polls."""
        if self._thread and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while self._stop is not None and not self._stop.is_set():
            try:
                self._check_once()
            except Exception:
                # Never crash the watcher thread; the user wants
                # quiet pair mode, not stack traces.
                pass
            self._stop.wait(self.interval)

    def _check_once(self) -> None:
        """One poll cycle. Test-friendly entry point."""
        if not self.path.exists():
            return
        try:
            stat = self.path.stat()
        except OSError:
            return
        mtime = stat.st_mtime
        now = time.monotonic()

        if mtime != self._last_mtime:
            # Detected change — start the debounce window.
            self._last_mtime = mtime
            self._last_change_at = now
            return

        # No new change. If a previous change is still within the
        # debounce window, fire the callback once it passes.
        if self._last_change_at > 0 and (now - self._last_change_at) >= self.debounce:
            try:
                content = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = ""
            try:
                self.on_change(self.path, content)
            finally:
                # Reset so we don't re-fire on the same edit.
                self._last_change_at = 0.0


def make_watcher(
    path: str | Path,
    on_change: Callable[[Path, str], None],
    *,
    interval: float = 0.5,
    debounce: float = 0.3,
) -> PollingWatcher:
    """Factory — returns the right watcher for the current platform.
    v1.35.8 always returns PollingWatcher; future versions may
    return a watchdog-backed implementation when available."""
    return PollingWatcher(
        path=Path(path),
        on_change=on_change,
        interval=interval,
        debounce=debounce,
    )
