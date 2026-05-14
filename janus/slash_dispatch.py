"""
slash_dispatch.py — v1.24.0 shared slash command registry.

PROBLEM:
Pre-v1.24 each surface (cli_rich, cli, tui) carried its own monolithic
dispatcher with a long if/elif chain. The command metadata
(name, description, category) was duplicated. Adding a new command
meant touching three files. The TUI in v1.23 only supported 5 of the
35 commands because re-implementing each one was prohibitive.

DESIGN (minimal-disruption):
This module owns the SHARED parts:

  * SlashCommand dataclass — single source of truth for command shape.
  * BUILTIN_COMMANDS — the canonical command catalogue (moved here
    from cli_rich.py; cli_rich re-exports for back-compat).
  * SlashRegistry — per-surface registry. Each surface registers its
    handlers; dispatch(line, ctx) looks up by name and invokes.
  * SlashContext — common arguments passed to every handler.
  * Slash arg parsing helpers.

Existing surface dispatchers (cli_rich.py:_dispatch, cli.py loop,
tui/app.py:_handle_slash) KEEP their if/elif bodies for v1.24.0 — we
don't migrate handler bodies in this release. Instead they OPT IN to
the registry by registering thin wrappers, so the TUI can route
unknown commands through the registry rather than printing
"unhandled slash".

Why minimum-viable?
A full migration would touch ~3700 lines across cli_rich/cli/tui and
break a lot of tests. Sam's goal is "stop duplicating the command
metadata" — that's achievable without rewriting every handler. v1.24.x
can deepen the migration command-by-command if desired.

USAGE FROM A SURFACE:

    from janus.slash_dispatch import SlashRegistry, BUILTIN_COMMANDS

    reg = SlashRegistry()
    reg.register("/mode", my_mode_handler)
    reg.register("/clear", my_clear_handler)
    ...

    # In the input loop:
    if line.startswith("/"):
        handled, output = reg.dispatch(line, ctx)
        if handled:
            return output

HANDLER SIGNATURE:
    def handler(ctx: SlashContext, arg: str) -> str | bool | None
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------- command metadata (the canonical catalogue) ----------


@dataclass(frozen=True)
class SlashCommand:
    """One entry in the slash-command palette.

    `category` drives both dropdown grouping and the colored marker dot:
    "built-in" (cyan) for hardcoded commands, "custom" (green) for files
    in `~/.janus/commands/` or `<workspace>/.janus/commands/`.
    """
    name: str
    description: str
    category: str = "built-in"


BUILTIN_COMMANDS: list[SlashCommand] = [
    SlashCommand("/mode",         "switch permission mode: default | acceptEdits | plan | auto | bypassPermissions", "built-in"),
    SlashCommand("/why",          "re-interpret your last message and show 2-3 candidate readings", "built-in"),
    SlashCommand("/workspace",    "show or change the active workspace directory",       "built-in"),
    SlashCommand("/analyze",      "scan the workspace for tools, skills, project hints", "built-in"),
    SlashCommand("/memory",       "memory: /memory [<cat>|search <q>|stats|show <id>|pause|resume|reindex|clear --type=<t>|prune|consolidate|audit|about-me]", "built-in"),
    SlashCommand("/interview",    "fill memory cards Q&A-style: /interview [<category>|daily [N]|pause]", "built-in"),
    SlashCommand("/search",       "search prior interactions in the log index",          "built-in"),
    SlashCommand("/skills",       "list/filter skills, or install-bundled to copy the starter catalog", "built-in"),
    SlashCommand("/promote",      "promote a quarantined skill to a trusted state",      "built-in"),
    SlashCommand("/skill",        "skill authoring — subcommands: new | review | import | gepa","built-in"),
    SlashCommand("/cost",         "show token + cost summary for this session",          "built-in"),
    SlashCommand("/project",      "show detected project type + indicators (v1.28.4)",   "built-in"),
    SlashCommand("/clear",        "clear conversation turns and cost counters",          "built-in"),
    SlashCommand("/compact",      "summarize and prune older turns in this conversation","built-in"),
    SlashCommand("/compress",     "alias for /compact",                                  "built-in"),
    SlashCommand("/retry",        "re-run the last user turn (drops last assistant reply)", "built-in"),
    SlashCommand("/undo",         "drop the last user+assistant pair from this conversation", "built-in"),
    SlashCommand("/insights",     "activity summary: /insights [days] (default 7)",      "built-in"),
    SlashCommand("/stats",        "rate-limit + token usage in the rolling 60s window",  "built-in"),
    SlashCommand("/pin",          "pin turn so /compact never drops it: /pin <N|last>",  "built-in"),
    SlashCommand("/unpin",        "unpin turn N (or 'last')",                            "built-in"),
    SlashCommand("/pins",         "list pinned turns in this conversation",              "built-in"),
    SlashCommand("/resume",       "resume a saved conversation by id",                   "built-in"),
    SlashCommand("/continue",     "continue the most recent conversation",               "built-in"),
    SlashCommand("/verbose",      "toggle verbose tool-arg display",                     "built-in"),
    SlashCommand("/stream",       "toggle token streaming on/off",                       "built-in"),
    SlashCommand("/init",         "scan codebase and propose starter user.md + skills",  "built-in"),
    SlashCommand("/model",        "show or set the model id for this session",           "built-in"),
    SlashCommand("/doctor",       "run diagnostics on configuration and environment",    "built-in"),
    SlashCommand("/output-style", "switch output rendering (markdown, plain, json, …)",  "built-in"),
    SlashCommand("/commands",     "list user-defined slash commands and their files",    "built-in"),
    SlashCommand("/eval",         "replay last N records at temp=0 to check stability",  "built-in"),
    SlashCommand("/mcp",          "MCP servers — list | catalog | tools <s> | inspect <s> <t> | connect | disconnect", "built-in"),
    SlashCommand("/triggers",     "list configured triggers",                            "built-in"),
    SlashCommand("/swarm",        "agent swarms — list | describe | run | status | cancel", "built-in"),
    SlashCommand("/goal",         "manage standing objective: /goal <text> | status | pause | resume | clear | budget <N>", "built-in"),
    SlashCommand("/agent",        "first-class agents — /agent list | /agent <name> <prompt>", "built-in"),
    SlashCommand("/kanban",       "multi-agent task board — /kanban add | list | show | done | fail | block | unblock | delete | status | start | stop", "built-in"),
    SlashCommand("/claude",       "shorthand for /agent claude <prompt> — delegate to Claude Code", "built-in"),
    SlashCommand("/help",         "show all available slash commands grouped by source", "built-in"),
    SlashCommand("/quit",         "exit the CLI",                                        "built-in"),
    SlashCommand("/exit",         "alias for /quit",                                     "built-in"),
    SlashCommand("/refresh",      "reload sidebar (TUI only)",                           "built-in"),
    SlashCommand("/grants",       "list / clear approval grants: /grants [list|clear|revoke <tool>]", "built-in"),
]


# Back-compat: a flat list of names for older call-sites.
SLASH_COMMANDS: list[str] = [c.name for c in BUILTIN_COMMANDS]


_CATEGORY_ORDER = {"built-in": 0, "custom": 1}


def all_slash_commands(custom_commands: dict | None) -> list[SlashCommand]:
    """Built-ins + customs, sorted by (category, name) for stable grouping.

    `custom_commands` is the surface-specific dict of user-defined
    commands (typically loaded from `~/.janus/commands/`). Each value
    must have a `description` attribute or key.
    """
    out = list(BUILTIN_COMMANDS)
    for name, cc in (custom_commands or {}).items():
        desc = (
            getattr(cc, "description", None)
            or (cc.get("description") if isinstance(cc, dict) else None)
            or "(no description)"
        )
        out.append(SlashCommand(
            name=f"/{name}" if not name.startswith("/") else name,
            description=desc,
            category="custom",
        ))
    return sorted(out, key=lambda c: (_CATEGORY_ORDER.get(c.category, 9), c.name))


def lookup(name: str) -> Optional[SlashCommand]:
    """Find a SlashCommand by name (with leading slash)."""
    for c in BUILTIN_COMMANDS:
        if c.name == name:
            return c
    return None


# ---------- registry + dispatch ----------


@dataclass
class SlashContext:
    """Common context passed to every slash handler.

    Surfaces fill in the fields they own. A handler reads only the
    fields it needs — the rest may be None.

    Mutating fields:
      `state` is a dict the handler can read AND write; it persists
      across handler calls within the same surface session.
    """
    surface: str = ""                       # "cli_rich" | "cli" | "tui" | "web"
    state: dict = field(default_factory=dict)
    console: Any = None                     # rich.Console for cli_rich; None for cli
    app: Any = None                         # textual App for tui; None elsewhere
    print_fn: Callable[[str], None] = print
    extra: dict = field(default_factory=dict)


HandlerResult = Optional[Any]   # handlers return True/False/None/str/dict
Handler = Callable[[SlashContext, str], HandlerResult]


class SlashRegistry:
    """Per-surface registry of slash command handlers.

    Multiple surfaces can have their own registries. Use the module-level
    `default_registry` if your surface doesn't need special handler
    customization.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        """Register a handler. Overwrites any existing handler for that name."""
        if not name.startswith("/"):
            name = "/" + name
        self._handlers[name] = handler

    def unregister(self, name: str) -> None:
        if not name.startswith("/"):
            name = "/" + name
        self._handlers.pop(name, None)

    def has(self, name: str) -> bool:
        if not name.startswith("/"):
            name = "/" + name
        return name in self._handlers

    def names(self) -> list[str]:
        return sorted(self._handlers.keys())

    def dispatch(
        self, line: str, ctx: SlashContext,
    ) -> tuple[bool, HandlerResult]:
        """Parse `line` (a slash command) and dispatch to its handler.

        Returns (handled, result). `handled=False` means no handler is
        registered for this command — caller falls through to the
        surface's legacy dispatcher.
        """
        if not line.startswith("/"):
            return (False, None)
        parts = line.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        h = self._handlers.get(cmd)
        if h is None:
            return (False, None)
        try:
            return (True, h(ctx, arg))
        except Exception as e:
            ctx.print_fn(f"[slash dispatch error] /{cmd[1:]}: {type(e).__name__}: {e}")
            return (True, None)


# ---------- helpers for surfaces (parsing common arg shapes) ----------


def split_subcommand(arg: str) -> tuple[str, str]:
    """`'foo bar baz' → ('foo', 'bar baz')`. Empty arg returns ('', '')."""
    if not arg:
        return ("", "")
    parts = arg.split(maxsplit=1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def parse_int_arg(arg: str, default: int = 0) -> int:
    try:
        return int(arg.strip())
    except (ValueError, AttributeError):
        return default


# ---------- v1.24.1: shared handlers (the migration starts here) ----------
#
# These handlers live in slash_dispatch.py (single source). Each surface
# (cli_rich / cli / tui) creates a SlashRegistry on startup, calls
# register_shared_handlers(reg), and adds its own surface-specific
# handlers on top.
#
# A handler returns a string (rendered to the user) or None (no output).
# Surfaces format the string however they like — Rich renders with a
# Panel, basic CLI prints raw, TUI writes to its log.


def _h_grants(ctx: SlashContext, arg: str) -> str:
    """v1.24.1: /grants list | clear | revoke <tool>

    Manage the persistent approval grants in ~/.janus/approvals.json
    plus this session's in-memory session grants.
    """
    from . import permissions
    ms = ctx.state.get("mode_state") if ctx.state else None
    if not isinstance(ms, permissions.ModeState):
        # Surface didn't wire in the mode state; fall back to a fresh
        # one (still loads persistent file correctly).
        ms = permissions.ModeState()
    sub, rest = split_subcommand(arg)
    sub = sub.lower().strip()
    if sub in ("", "list", "ls"):
        session_set, persistent_set = ms.list_grants()
        # Session grants minus persistent (persistent are auto-copied
        # into session_grants by has_grant lookup; subtract for clarity).
        session_only = session_set - persistent_set
        lines = []
        if not session_only and not persistent_set:
            return "no approval grants. earn them via [s]ession or [a]lways at the next approval prompt."
        if persistent_set:
            lines.append("persistent (~/.janus/approvals.json):")
            for tool, risk in sorted(persistent_set):
                lines.append(f"  {tool:24} {risk}")
        if session_only:
            if lines:
                lines.append("")
            lines.append("session-only (cleared on exit):")
            for tool, risk in sorted(session_only):
                lines.append(f"  {tool:24} {risk}")
        return "\n".join(lines)
    if sub in ("clear", "wipe"):
        ms.clear_persistent()
        ms.clear_grants()
        return "all grants cleared (persistent + session)."
    if sub in ("revoke", "remove", "rm"):
        target = rest.strip()
        if not target:
            return "usage: /grants revoke <tool_name>"
        # Revoke any grant whose tool matches.
        _, persistent = ms.list_grants()
        removed = 0
        for tool, risk in list(persistent):
            if tool == target:
                ms.revoke_persistent((tool, risk))
                removed += 1
        # Also drop session grants matching the same tool.
        for tool, risk in list(ms.session_grants):
            if tool == target:
                ms.session_grants.discard((tool, risk))
                removed += 1
        return f"revoked {removed} grant(s) for {target}." if removed \
            else f"no grants matched {target}."
    return (
        "usage: /grants [list|clear|revoke <tool>]\n"
        "  list             show session + persistent grants (default)\n"
        "  clear            wipe ALL grants (persistent + session)\n"
        "  revoke <tool>    drop grants for a specific tool"
    )


# v1.36.0 — Phase 8.1: 5 more cross-surface handlers (proof of pattern).
# Migration approach: any read-only / info-only command moves here
# first. State-mutating commands (/clear, /mode, etc.) stay surface-
# specific until session-state ownership is unified.


def _h_version(ctx, arg) -> str:
    """`/version` — print the running Janus version."""
    from . import branding
    return f"janus {branding.VERSION}  ({branding.TAGLINE})"


def _h_cwd(ctx, arg) -> str:
    """`/cwd` — print the workspace directory janus operates in."""
    from . import config
    return str(config.WORKSPACE)


def _h_home(ctx, arg) -> str:
    """`/home` — print the ~/.janus state directory path."""
    from . import config
    return str(config.HOME)


def _h_uptime(ctx, arg) -> str:
    """`/uptime` — print this session's uptime in human-readable form."""
    import time
    started = ctx.state.get("_session_start_ts") if hasattr(ctx, "state") else None
    if started is None:
        return "uptime: (session start not tracked on this surface)"
    secs = int(time.time() - started)
    if secs < 60:
        return f"uptime: {secs}s"
    if secs < 3600:
        return f"uptime: {secs // 60}m {secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"uptime: {h}h {rem // 60}m"


def _h_provider(ctx, arg) -> str:
    """`/provider` — show the detected LLM provider + cache support."""
    from . import llm, config
    p = llm.detect_provider(config.API_BASE)
    cache_ok = llm.cache_supported(p)
    cache_str = "cache supported" if cache_ok else "cache markers ignored"
    return f"provider: {p}  ({cache_str})  base: {config.API_BASE}"


# ---------- v1.37.0 — Phase 10.1.0: /goal Ralph Loop primitive ----------
#
# /goal sets a standing objective the agent works toward across turns.
# v10.1.0 ships ONLY the state primitive + slash commands. The auto-
# continue loop + judge model lands in v10.1.1.
#
# Subcommands:
#   /goal <text>      — set a new goal (replaces any existing)
#   /goal             — show status (== /goal status)
#   /goal status      — show the current goal + remaining turn budget
#   /goal pause       — pause the active goal
#   /goal resume      — resume a paused goal
#   /goal clear       — drop the goal entirely
#   /goal budget <N>  — adjust the turn budget on the active goal
#
# Scope: each surface declares its session identity. cli_rich uses the
# fixed string 'cli_rich'; future telegram/web pass scope via
# ctx.extra['goal_scope'] so the same handler serves all surfaces.


def _goal_scope(ctx: SlashContext) -> str:
    """Resolve the storage scope for this surface's goals.

    Surfaces that have multi-session shape (telegram per chat_id,
    web per session_id) pass `ctx.extra['goal_scope']`. Surfaces
    without that fall back to the surface name — works for cli_rich
    where there's only one session at a time.
    """
    if ctx.extra:
        s = ctx.extra.get("goal_scope")
        if s:
            return str(s)
    return ctx.surface or "default"


def _queue_goal_kickoff(ctx: SlashContext, goal_text: str) -> None:
    """v1.41.9 — queue an auto-continue prompt so the very next REPL
    iteration starts working on the goal. Without this, the user has
    to type something manually after /goal to bootstrap the loop.

    Idempotent: skips if something else has already queued an input.
    No-op on surfaces (Telegram) that don't honor state["__auto_continue_input__"].
    """
    if ctx is None or ctx.state is None:
        return
    if ctx.state.get("__auto_continue_input__"):
        return
    ctx.state["__auto_continue_input__"] = (
        f"Start working toward the standing goal: {goal_text}\n"
        f"What's the first concrete step? "
        f"Take it now if it's safe to do so."
    )


def _h_goal(ctx: SlashContext, arg: str) -> str:
    """`/goal` — manage the standing objective for this scope."""
    from . import goals as _g

    scope = _goal_scope(ctx)
    sub, rest = split_subcommand(arg)
    sub_lower = sub.lower().strip()

    # Bare `/goal` → status
    if not arg.strip():
        return _g.format_status(_g.load(scope))

    # Reserved subcommand keywords come first; anything else is goal text.
    if sub_lower in ("status", "stat"):
        return _g.format_status(_g.load(scope))

    if sub_lower in ("pause",):
        g = _g.pause(scope)
        if g is None:
            return "no goal set."
        if g.status == "paused":
            return f"goal paused.\n{_g.format_status(g)}"
        return f"goal already {g.status}.\n{_g.format_status(g)}"

    if sub_lower in ("resume", "continue"):
        g = _g.resume(scope)
        if g is None:
            return "no goal set."
        if g.status == "active":
            # v1.41.9 — kick off the very next turn automatically.
            # See the long comment near `set_goal` below for why.
            _queue_goal_kickoff(ctx, g.text)
            return f"goal resumed.\n{_g.format_status(g)}"
        return f"goal is {g.status}, can't resume.\n{_g.format_status(g)}"

    if sub_lower in ("clear", "drop", "cancel"):
        existed = _g.clear(scope)
        return "goal cleared." if existed else "no goal to clear."

    if sub_lower in ("budget",):
        try:
            new_budget = int(rest.strip())
        except (ValueError, AttributeError):
            return "usage: /goal budget <N>   (positive integer)"
        if new_budget <= 0:
            return "budget must be a positive integer."
        g = _g.load(scope)
        if g is None:
            return "no goal set."
        g.turn_budget = new_budget
        _g.save(scope, g)
        return f"budget updated to {new_budget}.\n{_g.format_status(g)}"

    # Anything else = goal text. Use the WHOLE arg (not split) so
    # multi-word goals like "/goal refactor the planner module" keep
    # all their words.
    text = arg.strip()
    g = _g.set_goal(scope, text)

    # v1.41.9 — kick off the very next turn automatically. Without
    # this, the auto-continue loop never starts because
    # goal_loop.after_turn() only fires AFTER an assistant turn —
    # and setting /goal from a slash command doesn't produce a turn.
    # The CLI's main loop pops state["__auto_continue_input__"] at
    # the top of each iteration; queuing the kickoff there is
    # exactly the same path used by post-turn judge continuations,
    # so the first turn looks identical to subsequent ones.
    # Surfaces without this state key (Telegram) just ignore it —
    # users there will still have to send one message to bootstrap.
    _queue_goal_kickoff(ctx, g.text)

    # v1.37.1 — Phase 10.1.1: plan-mode auto-leave. A goal in plan
    # mode is a contradiction (plan blocks writes; the loop needs
    # to make changes). Flip to default and tell the user. Sam's
    # 2026-05-10 design call: auto-leave > refuse, since refuse
    # would force an extra step every time.
    plan_msg = ""
    ms = ctx.state.get("mode_state") if ctx.state else None
    try:
        from . import permissions as _perm
        if ms is not None and getattr(ms, "current", None) == _perm.PLAN:
            ms.current = _perm.DEFAULT
            plan_msg = (
                "\nleft plan mode (default) — /goal needs write "
                "access to make progress. Re-enter with /mode plan."
            )
    except Exception:
        pass

    return (
        f"goal set: {g.text}\n"
        f"turn budget: {g.turn_budget}    "
        f"(auto-continue fires after every assistant turn — "
        f"/goal pause to halt)"
        f"{plan_msg}"
    )


def _h_kanban(ctx: SlashContext, arg: str) -> str:
    """v1.42.0 — multi-agent task board (Hermes-style Kanban).

    Subcommands:
      /kanban add <title> [--agent <name>] [--workspace <path>]
                          [--parent <id>] [--description <text>]
                          [--prompt <text>]   add a new task
      /kanban list [--status <state>] [--agent <name>]   list tasks
      /kanban show <id>                                  show one task
      /kanban done <id> [--output <text>]                mark COMPLETED
      /kanban fail <id> [--error <text>]                 mark FAILED
      /kanban block <id>                                 pause
      /kanban unblock <id>                               resume
      /kanban delete <id>                                remove
      /kanban status                                     counts + dispatcher state
      /kanban start                                      start the dispatcher
      /kanban stop                                       stop the dispatcher
    """
    from .kanban import store as _ks
    from .kanban import state as _kst
    from .kanban import dispatcher as _kd

    sub, rest = split_subcommand(arg)
    sub = sub.lower().strip()

    # `/kanban` with no args → quick status overview.
    if not arg.strip() or sub in ("status", "stat"):
        counts = _ks.counts_by_status()
        running = _kd.is_running()
        lines = [f"kanban dispatcher: {'running' if running else 'stopped'}"]
        for s in (_kst.BACKLOG, _kst.READY, _kst.IN_PROGRESS,
                  _kst.BLOCKED, _kst.COMPLETED, _kst.FAILED):
            lines.append(f"  {s:<12} {counts.get(s, 0)}")
        return "\n".join(lines)

    if sub == "start":
        _kd.start()
        return "kanban dispatcher started."
    if sub == "stop":
        _kd.stop()
        return "kanban dispatcher stopped."

    if sub == "list":
        flt_status, flt_agent = _parse_kv_flags(
            rest, allowed={"status", "agent"},
        )
        tasks = _ks.list_tasks(status=flt_status, agent_profile=flt_agent)
        if not tasks:
            return "no tasks. /kanban add <title> --agent <name> to start."
        lines = []
        for t in tasks:
            deps = f" (deps: {t.parent_ids})" if t.parent_ids else ""
            ws = f" [{t.workspace}]" if t.workspace else ""
            lines.append(
                f"  #{t.id:<3} [{t.status:<11}] @{t.agent_profile:<10} "
                f"{t.title}{deps}{ws}"
            )
        return "\n".join(lines)

    if sub == "show":
        try:
            tid = int(rest.strip().split()[0])
        except (ValueError, IndexError):
            return "usage: /kanban show <id>"
        t = _ks.get_task(tid)
        if not t:
            return f"task #{tid} not found."
        body = (
            f"#{t.id} [{t.status}] @{t.agent_profile}\n"
            f"  title:       {t.title}\n"
            f"  description: {t.description or '-'}\n"
            f"  workspace:   {t.workspace or '-'}\n"
            f"  prompt:      {t.prompt or '-'}\n"
            f"  parents:     {t.parent_ids or '-'}\n"
            f"  created:     {t.created_at}\n"
        )
        if t.claimed_at:
            body += f"  claimed:     {t.claimed_at}\n"
        if t.completed_at:
            body += f"  completed:   {t.completed_at}\n"
        if t.last_error:
            body += f"  last_error:  {t.last_error}\n"
        if t.output:
            body += f"  output:      {t.output[:300]}{'…' if len(t.output) > 300 else ''}\n"
        return body.rstrip()

    if sub == "add":
        parsed = _parse_kanban_add(rest)
        if isinstance(parsed, str):
            return parsed  # error message
        try:
            t = _ks.create_task(**parsed)
        except ValueError as e:
            return f"error: {e}"
        return (
            f"created #{t.id} [{t.status}] @{t.agent_profile}: {t.title}"
            + (f" (parents: {t.parent_ids})" if t.parent_ids else "")
        )

    if sub in ("done", "complete"):
        return _kanban_quick_status(rest, _kst.COMPLETED)
    if sub == "fail":
        return _kanban_quick_status(rest, _kst.FAILED)
    if sub == "block":
        return _kanban_quick_status(rest, _kst.BLOCKED)
    if sub in ("unblock", "resume"):
        return _kanban_quick_status(rest, _kst.BACKLOG)

    if sub in ("delete", "rm"):
        try:
            tid = int(rest.strip().split()[0])
        except (ValueError, IndexError):
            return "usage: /kanban delete <id>"
        return (
            f"deleted #{tid}." if _ks.delete_task(tid)
            else f"task #{tid} not found."
        )

    return (
        f"unknown subcommand: {sub!r}. "
        f"try /kanban (no args) for status, or see /help kanban."
    )


def _parse_kv_flags(
    text: str, *, allowed: set,
) -> tuple[str | None, str | None]:
    """Parse `--status X --agent Y` style flags. Returns (status, agent)."""
    status_val: str | None = None
    agent_val: str | None = None
    parts = text.strip().split()
    i = 0
    while i < len(parts):
        tok = parts[i]
        if tok == "--status" and i + 1 < len(parts):
            status_val = parts[i + 1]
            i += 2
            continue
        if tok == "--agent" and i + 1 < len(parts):
            agent_val = parts[i + 1]
            i += 2
            continue
        i += 1
    return status_val, agent_val


def _parse_kanban_add(text: str):
    """Parse the `/kanban add` flag soup. Returns a kwargs dict for
    store.create_task, or an error string on bad input.

    Title is everything before the first `--flag` (or the whole string
    if no flags). Flags: --agent, --workspace, --parent, --description,
    --prompt, --max-retries.
    """
    import shlex
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        return f"error parsing args: {e}"
    title_words: list[str] = []
    kwargs: dict[str, object] = {
        "agent_profile": "developer",
        "parent_ids": [],
    }
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            flag = tok[2:]
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            if flag == "agent":
                kwargs["agent_profile"] = val
            elif flag == "workspace":
                kwargs["workspace"] = val
            elif flag == "description":
                kwargs["description"] = val
            elif flag == "prompt":
                kwargs["prompt"] = val
            elif flag == "parent":
                try:
                    kwargs["parent_ids"].append(int(val))  # type: ignore[union-attr]
                except ValueError:
                    return f"error: --parent expects an integer, got {val!r}"
            elif flag in ("max-retries", "max_retries"):
                try:
                    kwargs["max_retries"] = int(val)
                except ValueError:
                    return f"error: --max-retries expects an integer"
            else:
                return f"error: unknown flag --{flag}"
            i += 2
        else:
            title_words.append(tok)
            i += 1
    title = " ".join(title_words).strip()
    if not title:
        return "usage: /kanban add <title> [--agent NAME] [--workspace PATH] [--parent ID]…"
    kwargs["title"] = title
    return kwargs


def _kanban_quick_status(rest: str, new_status: str) -> str:
    """Shared body for /kanban done | fail | block | unblock."""
    from .kanban import store as _ks
    try:
        tid = int(rest.strip().split()[0])
    except (ValueError, IndexError):
        return "usage: /kanban <done|fail|block|unblock> <id>"
    extra: dict = {}
    after = rest.strip().split(None, 1)
    if len(after) > 1 and after[1].startswith("--"):
        flag, _, val = after[1][2:].partition(" ")
        if flag == "output":
            extra["output"] = val
        elif flag == "error":
            extra["last_error"] = val
    try:
        t = _ks.set_status(tid, new_status, **extra)
    except ValueError as e:
        return f"error: {e}"
    return f"#{t.id} → [{t.status}]"


def _h_agent(ctx: SlashContext, arg: str) -> str:
    """v1.41.0: /agent list | /agent <name> <prompt>

    Surfaces the new janus.agents abstraction (Phase 11.0). Each
    agent has Identity / Memory / Tools / Skills and is dispatched
    via janus.agents.dispatch().
    """
    from . import agents
    sub, rest = split_subcommand(arg)
    if not sub or sub == "list":
        all_agents = agents.list_agents()
        if not all_agents:
            return (
                "No agents discovered. Bundled live in "
                "janus/agents/bundled/; user-defined in ~/.janus/agents/."
            )
        lines = ["Available agents:"]
        for a in all_agents:
            d = a.to_dict()
            tools = ", ".join(d["tool_names"]) or "(none)"
            lines.append(
                f"  /agent {d['name']:<12} [{d['style']:<7}] tools=({tools})"
            )
            if d["description"]:
                lines.append(f"      {d['description']}")
        return "\n".join(lines)

    # /agent <name> <prompt>
    name = sub
    prompt = rest.strip()
    if not prompt:
        return f"usage: /agent {name} <prompt>"
    return agents.dispatch(name, prompt)


def _h_claude(ctx: SlashContext, arg: str) -> str:
    """v1.41.0: /claude <prompt> — shorthand for /agent claude <prompt>."""
    from . import agents
    prompt = (arg or "").strip()
    if not prompt:
        return "usage: /claude <prompt>"
    return agents.dispatch("claude", prompt)


def register_shared_handlers(registry: SlashRegistry) -> None:
    """Register the v1.24.1 shared handlers on a surface's registry.

    Surfaces call this during startup AFTER constructing their registry,
    so the shared handlers sit alongside surface-specific ones. A
    surface can override any shared handler by registering its own
    AFTER calling this — registry.register overwrites.

    v1.36.0 — Phase 8.1: added /version, /cwd, /home, /uptime, /provider
    as proof-of-pattern for the slash dispatcher migration. Five more
    commands sourced from one place; surfaces no longer duplicate them.

    v1.37.0 — Phase 10.1.0: added /goal (Ralph Loop primitive). State
    is filesystem-backed (~/.janus/goals/<scope>.json) so all surfaces
    read the same goal. Auto-continue loop arrives in v10.1.1.
    """
    registry.register("/grants", _h_grants)
    registry.register("/version", _h_version)
    registry.register("/cwd", _h_cwd)
    registry.register("/home", _h_home)
    registry.register("/uptime", _h_uptime)
    registry.register("/provider", _h_provider)
    registry.register("/goal", _h_goal)
    registry.register("/agent", _h_agent)
    registry.register("/claude", _h_claude)
    registry.register("/kanban", _h_kanban)
