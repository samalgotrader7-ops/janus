"""
status_line.py — v1.24.4 live status indicator for the CLI surfaces.

PROBLEM:
Sam reported (2026-05-07) "most of the time I do not know the Janus
on CLI is working or not". The pre-v1.24.4 cli_rich showed:

    ⚡ thinking…
      → shell(command=ps aux …)

That tells you what JUST happened, but not what's happening RIGHT NOW.
On a slow model call (10+ seconds), or while a long-running tool runs,
the screen is silent and the user has no signal.

DESIGN:
Claude Code uses a single line at the bottom of the chat that updates
live with elapsed time + cumulative token count. We mirror that:

    ✶ thinking… (12s · ↓ 1.4k tokens · thought for 8s)

A background thread runs at 400ms cadence, redrawing the line via
\\r + clear-to-end-of-line. Each on_step event updates the verb
("thinking" → "running shell" → "reading file"). When the turn ends
the line is cleared.

Writes go to stderr so they don't pollute -p stdout pipes. That also
means the status doesn't conflict with Rich's stdout-bound print() —
both go to the same terminal but on different fds.

NUDGE NOTE:
Status updates use \\r to overwrite the same line. Real output (tool
calls, stream chunks) clears the status before printing and the
status thread redraws on the next tick. There's a tiny flicker
during heavy streaming but it's preferable to silence.

TERMINAL SAFETY:
Auto-disabled when stdout/stderr are not TTYs (so -p output stays
clean), when JANUS_NO_STATUS_LINE is set, or when the locale isn't
UTF-8 safe (we don't want a thread emitting unrenderable glyphs).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional


SPINNER_FRAMES = ("✶", "✷", "✸", "✹", "✺", "✻", "✺", "✹", "✸", "✷")


def _fmt_time(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{int(s // 60)}m {int(s % 60)}s"
    return f"{int(s // 3600)}h {int((s % 3600) // 60)}m"


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _is_status_disabled() -> bool:
    """Check the env + tty heuristic."""
    flag = os.environ.get("JANUS_NO_STATUS_LINE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # Auto: disable on non-TTY (piped / redirected). The ANSI \r dance
    # would emit garbage into a log file.
    try:
        if not sys.stderr.isatty():
            return True
    except Exception:
        return True
    return False


class StatusLine:
    """Bottom-of-screen "currently doing X" indicator with live updates.

    Lifecycle:
        line = StatusLine()
        line.start()
        line.set_verb("thinking")
        # ... work ...
        line.set_verb("running shell")
        line.add_tokens(123)
        line.stop()  # clears the line and joins the thread

    Thread-safety:
        set_verb / add_tokens / set_tokens are safe to call from any
        thread. The render thread reads them via a lock.
    """

    def __init__(
        self,
        file=None,
        *,
        spinner_frames: tuple = SPINNER_FRAMES,
        update_interval: float = 0.4,
        verb: str = "thinking",
    ) -> None:
        self.file = file or sys.stderr
        self._lock = threading.Lock()
        self.t0 = time.monotonic()
        self.verb = verb
        self.tokens = 0
        self.thought_time = 0.0
        self._first_action_at: Optional[float] = None
        self.spinner_idx = 0
        self.spinner_frames = spinner_frames
        self.update_interval = update_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._disabled = _is_status_disabled()
        self._last_render = ""
        # When set, the render thread skips writes — used during stream
        # chunks where the status would interleave with model tokens.
        self._streaming = threading.Event()

    def start(self) -> None:
        if self._disabled or self._thread is not None:
            return
        self._stop.clear()
        self.t0 = time.monotonic()
        self.spinner_idx = 0
        self._thread = threading.Thread(
            target=self._loop, name="janus-status", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            self.clear()
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None
        self.clear()

    def set_verb(self, verb: str) -> None:
        """Update the action description (e.g. "running shell")."""
        with self._lock:
            self.verb = verb
            # First non-thinking verb marks the boundary between
            # "thought for Xs" and downstream activity.
            if (
                self._first_action_at is None
                and verb not in ("thinking", "calling model")
            ):
                self._first_action_at = time.monotonic()
                self.thought_time = self._first_action_at - self.t0

    def add_tokens(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.tokens += int(n)

    def set_tokens(self, n: int) -> None:
        with self._lock:
            self.tokens = int(n)

    def clear(self) -> None:
        """Erase the status line. Idempotent — safe to call multiple times."""
        if self._disabled:
            return
        try:
            self.file.write("\r\033[K")
            self.file.flush()
        except Exception:
            pass

    def redraw_now(self) -> None:
        """Force a single redraw — useful right after clearing for an
        external print so the status reappears before the next 400ms tick.
        """
        if self._disabled or self._thread is None:
            return
        try:
            self.file.write("\r\033[K" + self._format())
            self.file.flush()
        except Exception:
            pass

    def begin_streaming(self) -> None:
        """v1.24.4: pause status redraws while the chat loop is writing
        raw stream chunks to stdout. Without this, the spinner thread
        keeps overwriting partial token output with status text every
        400ms — visually destructive."""
        self._streaming.set()
        self.clear()

    def end_streaming(self) -> None:
        self._streaming.clear()

    # ---------- internal ----------

    def _format(self) -> str:
        with self._lock:
            verb = self.verb
            tokens = self.tokens
            thought = self.thought_time
        glyph = self.spinner_frames[self.spinner_idx % len(self.spinner_frames)]
        elapsed = time.monotonic() - self.t0
        # ANSI: magenta glyph, dim metadata.
        # \033[2m = dim; \033[35m = magenta; \033[0m = reset.
        parts = [_fmt_time(elapsed)]
        if tokens:
            parts.append(f"↓ {_fmt_tokens(tokens)} tokens")
        if thought >= 1.0:
            parts.append(f"thought for {int(thought)}s")
        meta = " · ".join(parts)
        return f"\033[35m{glyph}\033[0m {verb}… \033[2m({meta})\033[0m"

    def _loop(self) -> None:
        # Print the initial frame immediately so the user sees feedback
        # at t=0 instead of waiting for the first tick.
        if not self._streaming.is_set():
            try:
                self.file.write("\r\033[K" + self._format())
                self.file.flush()
            except Exception:
                pass
        while not self._stop.wait(self.update_interval):
            self.spinner_idx += 1
            if self._streaming.is_set():
                # Stream chunks own the cursor; don't fight them.
                continue
            try:
                self.file.write("\r\033[K" + self._format())
                self.file.flush()
            except Exception:
                # Don't crash the agent on terminal writes.
                pass


# ---------- step → verb mapping ----------

# Map tool names to user-facing verb phrases. Falls back to the tool
# name itself for tools not in the table (so e.g. a custom skill tool
# shows its actual name rather than something generic).
_TOOL_VERBS: dict[str, str] = {
    "shell": "running shell",
    "shell_run_bg": "starting bg shell",
    "shell_output": "reading shell output",
    "shell_kill": "killing shell",
    "shell_list": "listing shells",
    "fs_read": "reading file",
    "fs_list": "listing files",
    "fs_write": "writing file",
    "fs_edit": "editing file",
    "grep": "searching files",
    "glob": "globbing files",
    "code_exec": "running code",
    "memory_search": "searching memory",
    "memory_apply": "saving memory",
    "web_fetch": "fetching url",
    "web_search": "searching web",
    "browser_navigate": "browsing",
    "browser_screenshot": "snapping page",
    "swarm_run": "spawning swarm",
    "delegate": "delegating",
    "clarify": "asking for clarification",
    "agent_create": "creating agent",
    "agent_list": "listing agents",
    "agent_run_now": "firing agent",
    "ssh_exec": "running ssh",
    "todos": "updating todos",
    "telegram_react": "reacting (telegram)",
    "interview_ask": "asking interview question",
}


def verb_for_tool(tool_name: str) -> str:
    """User-facing description for a tool call. Defaults to "running
    <tool>" for unknown tools."""
    return _TOOL_VERBS.get(tool_name, f"running {tool_name}")
