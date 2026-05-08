"""Tests for v1.31.6 — pause status line during approval prompts.

FIELD-VALIDATION FINDING (Sam, 2026-05-08, on his Ubuntu VPS):

After v1.31.5 fixed the approver gating, the cyan-bordered Plan
Review panel rendered correctly. But the ``[Y]es proceed [N]o
refine >`` prompt was invisible — Sam waited ~7 minutes thinking
the system was hung, status line at the bottom showing ``running
exit_plan_mode… (2m 18s)``.

ROOT CAUSE:
``status_line.StatusLine`` runs a background thread that emits
``\\r\\033[K + status text`` to stderr every 400ms. When the
approver calls ``prompt_toolkit.prompt(...)``, the prompt renders
once — then 400ms later the status line redraw wipes it. The
prompt and status line fight for the same line in the terminal;
status wins because it redraws every tick.

THE FIX:
Wrap every ``prompt_toolkit.prompt`` call in the cli_rich approver
with ``sl.begin_streaming()`` / ``sl.end_streaming()``. The
``begin_streaming`` flag tells the StatusLine render loop to skip
its tick (it's the same primitive used during model token
streaming for the same reason).

Two approver prompts are wrapped:
  * The v1.31.5 plan-review prompt (where Sam saw the bug)
  * The standard ASK-path prompt (same latent bug, less noticeable
    because users decide write/exec faster than they read plans)

DESIGN INVARIANT PINNED:
  * StatusLine MUST pause before any user-input prompt and resume
    after. Without this, the spinner overwrites the prompt.
  * If the status line isn't available (state=None, headless,
    fallback path), the wrap is a no-op — falls through to the
    legacy behavior.
"""

from __future__ import annotations

import inspect

from janus import cli_rich, permissions


# ============================================================
# Source pins
# ============================================================


def test_make_mode_approver_accepts_state_kwarg():
    """v1.31.6: state must be a kwarg so the approver can fetch
    the active StatusLine for pause/resume."""
    sig = inspect.signature(cli_rich._make_mode_approver)
    p = sig.parameters.get("state")
    assert p is not None, "state kwarg missing from _make_mode_approver"
    # kwarg-only — comes after *
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is None  # back-compat: optional


def test_plan_review_prompt_pauses_status_line():
    """The plan-review prompt block calls begin_streaming /
    end_streaming around the prompt_toolkit prompt."""
    src = inspect.getsource(cli_rich._make_mode_approver)
    # Find the v1.31.5 plan-review block (the one rendering the
    # cyan panel via render_rich_panel).
    panel_idx = src.find("render_rich_panel(")
    assert panel_idx != -1
    region = src[panel_idx:panel_idx + 3500]
    assert "begin_streaming" in region
    assert "end_streaming" in region
    # And the v1.31.6 marker for context.
    assert "v1.31.6" in region


def test_ask_path_prompt_also_pauses_status_line():
    """Same fix on the standard ASK-path prompt (write/exec
    approval). Same root cause; less visible for quick decisions
    but the bug is identical."""
    src = inspect.getsource(cli_rich._make_mode_approver)
    # Find the standard prompt block ([Y]es [s]ession [a]lways)
    standard_idx = src.find("[Y]es  [s]ession")
    assert standard_idx != -1
    region = src[standard_idx:standard_idx + 2500]
    assert "begin_streaming" in region
    assert "end_streaming" in region


def test_status_line_pause_uses_state_dict():
    """The pause looks up the StatusLine via
    ``state.get("_status_line")`` — same key the rest of cli_rich
    uses to track the active spinner."""
    src = inspect.getsource(cli_rich._make_mode_approver)
    assert 'state.get("_status_line")' in src


def test_status_line_pause_is_defensive():
    """If state is None, or _status_line is None, or
    begin_streaming raises, the prompt path must still run.
    NEVER lock the user out because the spinner had a hiccup."""
    src = inspect.getsource(cli_rich._make_mode_approver)
    # try/except wrappers around the begin_streaming + end_streaming
    # calls, plus a sl is not None guard.
    plan_block = src[src.find("render_rich_panel("):src.find("render_rich_panel(") + 3500]
    assert "if sl is not None:" in plan_block
    # Wrapped in try/except (don't crash on a flaky terminal write)
    assert "except Exception:" in plan_block


def test_finally_resumes_status_line():
    """Even on EOFError / KeyboardInterrupt / unexpected exception,
    the StatusLine must be resumed. Use try/finally."""
    src = inspect.getsource(cli_rich._make_mode_approver)
    plan_block = src[src.find("render_rich_panel("):src.find("render_rich_panel(") + 3500]
    # The structure should be:
    #   begin_streaming
    #   try:
    #     prompt
    #   finally:
    #     end_streaming
    assert "finally:" in plan_block


# ============================================================
# Behavioral
# ============================================================


class _FakeConsole:
    def __init__(self):
        self.calls = []
    def print(self, *a, **kw):
        self.calls.append(("print", a, kw))


class _FakeStatusLine:
    """Captures begin_streaming / end_streaming calls."""
    def __init__(self):
        self.events: list[str] = []
    def begin_streaming(self) -> None:
        self.events.append("begin")
    def end_streaming(self) -> None:
        self.events.append("end")


def test_plan_prompt_pauses_status_line_around_input(monkeypatch):
    """End-to-end: the approver, when given a plan action and a
    state with _status_line set, must call begin_streaming BEFORE
    the prompt and end_streaming AFTER."""
    sl = _FakeStatusLine()
    state = {"_status_line": sl}
    mode_state = permissions.ModeState()
    mode_state.set("plan")
    console = _FakeConsole()

    # The prompt itself must be called between begin/end. We
    # capture the order by recording the prompt invocation in the
    # same events list.
    def fake_prompt(prompt_text, default=""):
        sl.events.append("prompt")
        return "n"

    monkeypatch.setattr(
        "prompt_toolkit.prompt", fake_prompt, raising=False,
    )

    approver = cli_rich._make_mode_approver(
        console, mode_state, state=state,
    )
    result = approver(
        "exit_plan_mode",
        "## Plan\n1. step\n2. step",
        risk="read",
    )
    assert result is False  # user declined
    # Order must be: begin, prompt, end
    assert sl.events == ["begin", "prompt", "end"], (
        f"status line not paused around prompt: {sl.events}"
    )


def test_no_status_line_no_crash(monkeypatch):
    """When state is None or _status_line is missing, the prompt
    still runs — no AttributeError, no crash."""
    mode_state = permissions.ModeState()
    mode_state.set("plan")
    console = _FakeConsole()

    monkeypatch.setattr(
        "prompt_toolkit.prompt",
        lambda *a, **kw: "n",
        raising=False,
    )

    # state=None case
    approver = cli_rich._make_mode_approver(
        console, mode_state, state=None,
    )
    result = approver("exit_plan_mode", "## P", risk="read")
    assert result is False  # user declined, no crash

    # state without _status_line case
    approver2 = cli_rich._make_mode_approver(
        console, mode_state, state={},
    )
    result2 = approver2("exit_plan_mode", "## P", risk="read")
    assert result2 is False  # user declined, no crash


def test_status_line_resumed_even_on_exception(monkeypatch):
    """If the prompt raises (EOFError / KeyboardInterrupt /
    unexpected), end_streaming must still fire. try/finally."""
    sl = _FakeStatusLine()
    state = {"_status_line": sl}
    mode_state = permissions.ModeState()
    mode_state.set("plan")
    console = _FakeConsole()

    def crashing_prompt(prompt_text, default=""):
        sl.events.append("prompt-crashed")
        raise EOFError("simulated Ctrl+D")

    monkeypatch.setattr(
        "prompt_toolkit.prompt", crashing_prompt, raising=False,
    )

    approver = cli_rich._make_mode_approver(
        console, mode_state, state=state,
    )
    result = approver("exit_plan_mode", "## P", risk="read")
    # EOFError caught → return False
    assert result is False
    # AND end_streaming was still called
    assert "end" in sl.events
    assert sl.events.index("begin") < sl.events.index("end")


def test_buggy_begin_streaming_does_not_block_prompt(monkeypatch):
    """If begin_streaming itself raises, the prompt should still
    run (degraded UX — user sees status overwrites — but they're
    not locked out)."""
    class _BuggyStatusLine:
        def __init__(self):
            self.end_called = False
        def begin_streaming(self):
            raise RuntimeError("flaky terminal")
        def end_streaming(self):
            self.end_called = True

    sl = _BuggyStatusLine()
    state = {"_status_line": sl}
    mode_state = permissions.ModeState()
    mode_state.set("plan")
    console = _FakeConsole()

    prompt_called = []
    monkeypatch.setattr(
        "prompt_toolkit.prompt",
        lambda *a, **kw: prompt_called.append(a) or "n",
        raising=False,
    )

    approver = cli_rich._make_mode_approver(
        console, mode_state, state=state,
    )
    result = approver("exit_plan_mode", "## P", risk="read")
    assert result is False
    # Prompt MUST have run despite the begin_streaming crash
    assert prompt_called, "prompt blocked by buggy begin_streaming"
