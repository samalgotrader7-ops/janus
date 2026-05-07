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
    SlashCommand("/skill",        "skill authoring — subcommands: new | review | import","built-in"),
    SlashCommand("/cost",         "show token + cost summary for this session",          "built-in"),
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
    SlashCommand("/mcp",          "manage MCP servers — list | connect | disconnect",    "built-in"),
    SlashCommand("/triggers",     "list configured triggers",                            "built-in"),
    SlashCommand("/swarm",        "agent swarms — list | describe | run | status | cancel", "built-in"),
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


def register_shared_handlers(registry: SlashRegistry) -> None:
    """Register the v1.24.1 shared handlers on a surface's registry.

    Surfaces call this during startup AFTER constructing their registry,
    so the shared handlers sit alongside surface-specific ones. A
    surface can override any shared handler by registering its own
    AFTER calling this — registry.register overwrites.
    """
    registry.register("/grants", _h_grants)
