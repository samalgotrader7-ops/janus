"""
janus/tui/app.py — Textual app for v1.23.

LAYOUT:
  +------------------------------------------------------------------+
  | mode: auto | tokens: 12.3k | cost: $0.42                         |  Header
  +------------+-----------------------------------------------------+
  | TOOLS      | Chat log (RichLog)                                  |
  | MEMORY     |                                                     |
  | SKILLS     |                                                     |
  | AGENTS     +-----------------------------------------------------+
  | LOGS       | Input (TextArea)                                    |
  +------------+-----------------------------------------------------+
  | [/] cmd  [Tab] focus  [Ctrl+Q] quit                              |  Footer

EXECUTOR INTEGRATION:
The Textual app runs in an asyncio loop. executor.chat() is sync, so
we invoke it via run_in_thread(). The approver replacement is a sync
function that pushes a Textual ModalScreen via call_from_thread() and
blocks on a threading.Event for the user's decision.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .. import app as janus_app  # JanusApp class shadows `app` import name
from .. import config, executor, memory, permissions, skills, branding
from ..tools import default_registry, make_protected, CapabilitySet


# ---------- approval modal ----------


class ApprovalModal(ModalScreen[bool]):
    """v1.23: non-blocking approval dialog. Returns True/False."""

    CSS = """
    ApprovalModal {
        align: center middle;
    }
    ApprovalModal #dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    ApprovalModal #title {
        text-style: bold;
        color: $primary;
        padding-bottom: 1;
    }
    ApprovalModal #risk {
        color: $warning;
        padding-bottom: 1;
    }
    ApprovalModal #details {
        color: $text;
        padding-bottom: 1;
    }
    ApprovalModal #buttons {
        align: right middle;
        height: auto;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("y", "approve", "approve"),
        Binding("n", "deny", "deny"),
        Binding("escape", "deny", "cancel"),
    ]

    def __init__(self, label: str, details: str, risk: str = "exec"):
        super().__init__()
        self._label = label
        self._details = details
        self._risk = risk

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"approve: {self._label}", id="title")
            yield Static(f"risk: {self._risk}", id="risk")
            yield Static(self._details, id="details")
            yield Static(
                "[y] approve   [n] deny   [esc] cancel",
                id="buttons",
            )

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class ClarifyModal(ModalScreen[str]):
    """v1.23: clarify dialog. Returns text (empty if cancelled)."""

    CSS = """
    ClarifyModal {
        align: center middle;
    }
    ClarifyModal #dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    ClarifyModal #q {
        text-style: bold;
        padding-bottom: 1;
    }
    ClarifyModal Input {
        margin-bottom: 1;
    }
    ClarifyModal #hint {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, question: str, choices: list[str] | None = None):
        super().__init__()
        self._question = question
        self._choices = list(choices or [])

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._question, id="q")
            if self._choices:
                yield Static("choices: " + " · ".join(self._choices))
            yield Input(placeholder="your answer...", id="answer")
            yield Static("[enter] submit  [esc] cancel", id="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(str(event.value or ""))

    def action_cancel(self) -> None:
        self.dismiss("")


# ---------- main app ----------


class JanusApp(App[None]):
    """v1.23: multi-pane Janus TUI."""

    CSS = """
    Screen { background: $background; }
    #content { height: 1fr; }
    #sidebar {
        width: 18;
        background: $boost;
        border-right: solid $primary 50%;
    }
    #main { width: 1fr; height: 1fr; }
    #chat-log { height: 1fr; padding: 0 1; }
    #input-row {
        dock: bottom;
        height: 5;
        background: $boost;
        padding: 1 1;
        border-top: solid $primary 50%;
    }
    #user-input { height: 3; }
    #stat-line {
        dock: top;
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "quit", priority=True),
        Binding("ctrl+l", "clear_chat", "clear"),
        Binding("ctrl+m", "cycle_mode", "cycle mode"),
    ]

    mode: reactive[str] = reactive("default")
    cost_usd: reactive[float] = reactive(0.0)
    tokens_total: reactive[int] = reactive(0)

    def __init__(self):
        super().__init__()
        self._messages: list[dict] = []
        self._busy = False
        self._tools_registry = None
        self._caps = CapabilitySet()
        self._loop: asyncio.AbstractEventLoop | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            self._stat_text(),
            id="stat-line",
        )
        with Horizontal(id="content"):
            with Vertical(id="sidebar"):
                with TabbedContent():
                    with TabPane("tools", id="tab-tools"):
                        yield ListView(id="list-tools")
                    with TabPane("memory", id="tab-memory"):
                        yield ListView(id="list-memory")
                    with TabPane("skills", id="tab-skills"):
                        yield ListView(id="list-skills")
            with Vertical(id="main"):
                yield RichLog(
                    id="chat-log",
                    highlight=False,
                    markup=True,
                    wrap=True,
                )
                with Vertical(id="input-row"):
                    yield Input(
                        placeholder="message janus... (enter to send, /help for commands)",
                        id="user-input",
                    )
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"janus {branding.VERSION}"
        self.mode = permissions.normalize(config.APPROVAL_MODE)
        self._loop = asyncio.get_event_loop()
        self._tools_registry = default_registry(capabilities=self._caps)
        self._refresh_sidebar()
        self._update_stat()
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold magenta]janus[/] {branding.VERSION} — type [bold]/help[/] for commands.")
        log.write("press [bold]Ctrl+Q[/] to quit, [bold]Ctrl+M[/] to cycle mode.")
        # Focus the input.
        self.query_one("#user-input", Input).focus()

    def _stat_text(self) -> str:
        return (
            f"mode: {self.mode}   "
            f"tokens: {self.tokens_total}   "
            f"cost: ${self.cost_usd:.4f}   "
            f"model: {config.MODEL}"
        )

    def _update_stat(self) -> None:
        try:
            stat = self.query_one("#stat-line", Static)
            stat.update(self._stat_text())
        except Exception:
            pass

    def watch_mode(self, *_: Any) -> None:
        self._update_stat()

    def watch_cost_usd(self, *_: Any) -> None:
        self._update_stat()

    def watch_tokens_total(self, *_: Any) -> None:
        self._update_stat()

    # ---------- sidebar refresh ----------

    def _refresh_sidebar(self) -> None:
        try:
            tools_list = self.query_one("#list-tools", ListView)
            tools_list.clear()
            if self._tools_registry is not None:
                for name in sorted(self._tools_registry.names()):
                    tools_list.append(ListItem(Label(name)))
            mem_list = self.query_one("#list-memory", ListView)
            mem_list.clear()
            try:
                from .. import memory_index
                rows = memory_index.list_all() or []
                for r in rows[:50]:
                    mem_list.append(ListItem(Label(
                        f"[{r.get('type','?')}] {r.get('subject','')[:40]}"
                    )))
            except Exception:
                pass
            sk_list = self.query_one("#list-skills", ListView)
            sk_list.clear()
            try:
                installed = skills.list_skills() or []
                for s in installed:
                    name = (s.get("name", "") if isinstance(s, dict)
                            else getattr(s, "name", ""))
                    state = (s.get("state", "") if isinstance(s, dict)
                             else getattr(s, "state", ""))
                    sk_list.append(ListItem(Label(f"{name} [{state}]")))
            except Exception:
                pass
        except Exception:
            # Initial mount race; sidebar refreshes again on next turn.
            pass

    # ---------- input handling ----------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "user-input":
            return
        text = (event.value or "").strip()
        if not text:
            return
        event.input.value = ""
        if text.startswith("/"):
            self._handle_slash(text)
            return
        self._send_to_executor(text)

    def _handle_slash(self, line: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        cmd, _, rest = line[1:].partition(" ")
        cmd = cmd.lower().strip()
        if cmd in ("help", "?"):
            # v1.24.0: list ALL commands from the shared registry, not
            # just the few the TUI has migrated. Commands not yet wired
            # in the TUI surface the existing "unhandled" message.
            from .. import slash_dispatch as _sd
            log.write("[bold]all slash commands[/] (TUI handles a subset; rest come in v1.24.x):")
            for entry in _sd.BUILTIN_COMMANDS:
                # Mark the TUI-supported commands distinctly.
                supported = entry.name in (
                    "/mode", "/clear", "/refresh", "/help", "/quit", "/exit", "/?",
                )
                marker = "[green]*[/]" if supported else "[dim] [/]"
                log.write(f"  {marker} [bold]{entry.name:<14}[/] — {entry.description}")
            log.write("\n[dim]* = handled in TUI; others fall through to a 'not yet wired' notice.[/]")
        elif cmd == "mode":
            new = (rest or "").strip()
            valid = ("default", "acceptEdits", "plan", "bypassPermissions", "auto")
            if new not in valid:
                log.write(f"[red]unknown mode '{new}'[/]; valid: {', '.join(valid)}")
            else:
                self.mode = new
                config.APPROVAL_MODE = new
                log.write(f"mode → [bold]{new}[/]")
        elif cmd == "clear":
            log.clear()
            self._messages = []
            log.write(f"[dim]chat cleared[/]")
        elif cmd == "refresh":
            self._refresh_sidebar()
            log.write("[dim]sidebar refreshed[/]")
        elif cmd in ("quit", "exit", "q"):
            self.exit()
        else:
            log.write(
                f"[yellow]unhandled slash:[/] /{cmd}\n"
                f"v1.23.x will route every cli_rich slash through the shared dispatcher."
            )

    def action_clear_chat(self) -> None:
        self._handle_slash("/clear")

    def action_cycle_mode(self) -> None:
        order = ("default", "acceptEdits", "plan", "bypassPermissions", "auto")
        try:
            cur = order.index(self.mode)
        except ValueError:
            cur = 0
        self.mode = order[(cur + 1) % len(order)]
        config.APPROVAL_MODE = self.mode

    # ---------- approver bridge (Textual ↔ executor thread) ----------

    def _make_approver(self):
        """Return a sync approver that pops up a ModalScreen in Textual."""

        def approver(action_label: str, details: str, **kw) -> bool:
            risk = kw.get("risk") or "exec"
            decision = permissions.decide(risk, self.mode)
            if decision == permissions.ALLOW:
                return True
            if decision == permissions.DENY:
                return False
            # ASK — pop a ModalScreen and block until user decides.
            return self._modal_approval(action_label, details, str(risk))

        return approver

    def _make_clarify_callback(self):
        def callback(question: str, choices):
            return self._modal_clarify(question, list(choices or []))
        return callback

    def _modal_approval(self, label: str, details: str, risk: str) -> bool:
        """Synchronously block until ApprovalModal dismisses.

        Called from the executor worker thread. Uses call_from_thread
        to push the modal onto Textual's event loop, and a
        threading.Event to wake on dismissal.
        """
        if self._loop is None:
            return False
        decision_holder = {"d": False}
        ev = threading.Event()

        def on_dismiss(decision):
            decision_holder["d"] = bool(decision)
            ev.set()

        def push():
            self.push_screen(
                ApprovalModal(label, details, risk),
                on_dismiss,
            )

        self.call_from_thread(push)
        ev.wait(timeout=1800)
        return decision_holder["d"]

    def _modal_clarify(self, question: str, choices: list[str]) -> str:
        if self._loop is None:
            return ""
        answer_holder = {"a": ""}
        ev = threading.Event()

        def on_dismiss(answer):
            answer_holder["a"] = str(answer or "")
            ev.set()

        def push():
            self.push_screen(
                ClarifyModal(question, choices),
                on_dismiss,
            )

        self.call_from_thread(push)
        ev.wait(timeout=1800)
        return answer_holder["a"]

    # ---------- send to executor ----------

    def _send_to_executor(self, user_input: str) -> None:
        if self._busy:
            return
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold cyan]you[/]: {user_input}")
        self._busy = True

        def runner():
            try:
                base_approver = self._make_approver()
                tools = self._tools_registry
                # Swap clarify with TUI-bound version.
                try:
                    from ..tools.clarify import Clarify as _Clarify
                    tools.remove_tool("clarify")
                    tools.add_tool(_Clarify(callback=self._make_clarify_callback()))
                except Exception:
                    pass
                approver = make_protected(base_approver, self._caps, self.mode)
                preamble = memory.prepend_for_prompt()
                t0 = time.time()
                # v1.25.7 Phase 0e: route through the substrate.
                output, trace = janus_app.run_turn(
                    messages=self._messages,
                    user_input=user_input,
                    tools=tools,
                    approver=approver,
                    memory_preamble=preamble,
                    mode=self.mode,
                    workspace=str(config.WORKSPACE),
                    tool_count=len(tools.names()),
                    skill_count=len(skills.list_skills()),
                    stream=False,
                )
                dt_ms = int((time.time() - t0) * 1000)
                self.call_from_thread(self._render_response, output, trace, dt_ms)
            except Exception as e:
                self.call_from_thread(self._render_error, str(e))
            finally:
                self.call_from_thread(self._mark_idle)

        threading.Thread(target=runner, daemon=True).start()

    def _render_response(self, output: str, trace: list[dict], dt_ms: int) -> None:
        log = self.query_one("#chat-log", RichLog)
        for step in trace:
            t = step.get("type", "")
            if t == "tool_call":
                log.write(
                    f"  [dim]→ {step.get('tool','?')} "
                    f"{str(step.get('args','')).replace(chr(10),' ')[:80]}[/]"
                )
            elif t == "soft_cap_warning":
                log.write(f"  [yellow]soft cap reached at step {step.get('step')}[/]")
            elif t == "step_limit_reached":
                log.write(f"  [red]step limit reached: {step.get('reason','')}[/]")
        log.write(f"[bold magenta]janus[/]: {output}\n[dim]({dt_ms} ms)[/]")
        # Refresh sidebar if anything mutated.
        self._refresh_sidebar()

    def _render_error(self, msg: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[red]error:[/] {msg}")

    def _mark_idle(self) -> None:
        self._busy = False
        self.query_one("#user-input", Input).focus()
