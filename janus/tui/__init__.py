"""
janus.tui — v1.23 multi-pane Textual TUI.

NEW IN v1.23:
A Textual-based TUI replaces the single-pane Rich loop in cli_rich.py
with a real layout:
  * Header bar (mode / tokens / cost / status)
  * Main column: chat log + tool ledger
  * Sidebar: tools / memory / skills / agents (tabs)
  * Footer: keybinds

Approval prompts use a non-blocking ModalScreen instead of synchronous
input(), so the event loop survives long-running tool calls.

ENTRY POINT:
  janus tui                       — launch the multi-pane TUI
  janus                           — still launches cli_rich (rich
                                    streaming chat, single pane)
  janus --basic                   — plain ANSI fallback (cli.py)

GRACEFUL DEGRADATION:
If textual isn't installed, `janus tui` prints a hint and exits non-zero.
The cli / cli_rich paths don't depend on textual.
"""
from __future__ import annotations


def serve() -> int:
    """Launch the TUI. Returns process exit code."""
    try:
        from .app import JanusApp
    except ImportError as e:
        print(
            "error: textual not installed.\n"
            "  install with: pipx install --include-deps 'janus-agent[tui]'\n"
            f"  or: pip install textual\n"
            f"  ({e})"
        )
        return 1
    app = JanusApp()
    try:
        app.run()
    except KeyboardInterrupt:
        return 0
    return 0
