"""
cli_rich.py — Phase 5: proper CLI on prompt_toolkit + rich.

Replaces the input()-based loop with:
  - prompt_toolkit for input: persistent history, slash-command autocomplete,
    multi-line input (Esc+Enter), live key bindings.
  - rich for output: panels, tables, syntax highlighting, markdown rendering.

GRACEFUL DEGRADATION:
If prompt_toolkit or rich aren't installed, this module raises ImportError
on call. The launcher (__main__.py) catches that and falls back to cli.main().
That way the CLI always works — `pip install prompt_toolkit rich` is
optional polish, not a hard dependency.
"""

from __future__ import annotations
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import config, interpreter, executor, logger, memory, index, skills
from . import eval as eval_mod, planner, orchestrator, skill_evolution
from . import skills_market, cache, branding, conversation, cost, statusline, skill_catalog
from . import commands as commands_mod, doctor, init_codebase, output_styles
from . import permissions
from .mcp import client as mcp_client
from .tools import default_registry, make_protected, CapabilitySet


# Imported lazily so import failure surfaces only when this CLI is selected.
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.markdown import Markdown
    from rich.text import Text
    HAVE_RICH = True
except ImportError:  # pragma: no cover
    HAVE_RICH = False
    Completer = object  # placeholder so subclass definition doesn't crash


@dataclass(frozen=True)
class SlashCommand:
    """One entry in the slash-command palette.

    `category` drives both dropdown grouping and the colored marker dot:
    "built-in" (cyan) for hardcoded commands, "custom" (green) for files
    in `~/.janus/commands/` or `<workspace>/.janus/commands/`.
    """
    name: str          # e.g. "/workspace"
    description: str
    category: str      # "built-in" | "custom"


BUILTIN_COMMANDS: list[SlashCommand] = [
    SlashCommand("/mode",         "switch permission mode: default | acceptEdits | plan | auto | bypassPermissions", "built-in"),
    SlashCommand("/why",          "re-interpret your last message and show 2-3 candidate readings", "built-in"),
    SlashCommand("/workspace",    "show or change the active workspace directory",       "built-in"),
    SlashCommand("/analyze",      "scan the workspace for tools, skills, project hints", "built-in"),
    SlashCommand("/memory",       "show all memory categories, or /memory <cat> for one (soul, user, project, …)", "built-in"),
    SlashCommand("/search",       "search prior interactions in the log index",          "built-in"),
    SlashCommand("/skills",       "list/filter skills, or install-bundled to copy the starter catalog", "built-in"),
    SlashCommand("/promote",      "promote a quarantined skill to a trusted state",      "built-in"),
    SlashCommand("/skill",        "skill authoring — subcommands: new | review | import","built-in"),
    SlashCommand("/cost",         "show token + cost summary for this session",          "built-in"),
    SlashCommand("/clear",        "clear conversation turns and cost counters",          "built-in"),
    SlashCommand("/compact",      "summarize and prune older turns in this conversation","built-in"),
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
]


# Back-compat: a flat list of names, still consulted by older call-sites.
SLASH_COMMANDS = [c.name for c in BUILTIN_COMMANDS]


_CATEGORY_DOT = {
    "built-in": "ansicyan",
    "custom":   "ansigreen",
}
_CATEGORY_ORDER = {"built-in": 0, "custom": 1}


def _all_slash_commands(customs: dict | None) -> list[SlashCommand]:
    """Built-ins + customs, sorted by (category, name) for stable grouping."""
    out = list(BUILTIN_COMMANDS)
    for name, cc in (customs or {}).items():
        out.append(SlashCommand(
            name=f"/{name}",
            description=cc.description or "(no description)",
            category="custom",
        ))
    return sorted(out, key=lambda c: (_CATEGORY_ORDER.get(c.category, 9), c.name))


if HAVE_RICH:
    # Dropdown styling. Defaults paint the menu with a gray fill and dim
    # the meta column so heavily that descriptions are unreadable on dark
    # terminals. We:
    #   - drop the gray fill (bg:default = inherit terminal bg),
    #   - use brand magenta for the selected row,
    #   - brighten the meta text so descriptions read clearly,
    #   - color the scrollbar so the "more entries below" affordance pops.
    JANUS_STYLE = Style.from_dict({
        "completion-menu":                         "bg:default",
        "completion-menu.completion":              "bg:default fg:#e6e6e6",
        "completion-menu.completion.current":      f"bg:{branding.BRAND_COLOR} fg:#ffffff bold",
        "completion-menu.meta.completion":         "bg:default fg:#b8c0cc",
        "completion-menu.meta.completion.current": f"bg:{branding.BRAND_COLOR} fg:#f5ecff",
        "scrollbar.background":                    "bg:default",
        "scrollbar.button":                        f"bg:{branding.BRAND_COLOR}",
        "scrollbar.arrow":                         f"fg:{branding.BRAND_COLOR}",
    })

    class SlashCompleter(Completer):
        """Autocomplete slash commands with descriptions + category coloring.

        `customs_provider` is a zero-arg callable returning the current
        custom-commands dict. We keep it as a callable (not a snapshot) so
        the dropdown reflects state changes — e.g. a future /reload that
        re-scans `~/.janus/commands/` mid-session works without rebuilding
        the completer.
        """

        def __init__(self, customs_provider: Callable[[], dict] | None = None):
            self._customs = customs_provider or (lambda: {})

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/promote "):
                stem = text[len("/promote "):]
                for s in skills.list_skills():
                    if s.name.startswith(stem):
                        yield Completion(
                            s.name,
                            start_position=-len(stem),
                            display_meta=f"{s.state} · {s.description[:60]}",
                        )
                return
            if text.startswith("/"):
                for cmd in _all_slash_commands(self._customs()):
                    if cmd.name.startswith(text):
                        dot_style = f"fg:{_CATEGORY_DOT.get(cmd.category, '')}"
                        display = FormattedText([
                            (dot_style, "● "),
                            ("", cmd.name),
                        ])
                        yield Completion(
                            cmd.name,
                            start_position=-len(text),
                            display=display,
                            # Leading spaces give the description column a
                            # visible gutter from the command name.
                            display_meta="  " + cmd.description,
                        )
else:
    SlashCompleter = None  # type: ignore[assignment]


def _need_libs():
    if not HAVE_RICH:
        raise ImportError(
            "cli_rich requires prompt_toolkit and rich. "
            "pip install prompt_toolkit rich  (or use `python -m janus --basic`)"
        )


# ---------- Helpers (rich-rendered) ----------


def _banner(console) -> None:
    """Bifurcation logo + status block + commands hint (rich-rendered)."""
    try:
        tool_count = len(default_registry().names())
    except Exception:
        tool_count = 0
    try:
        skill_count = len(skills.list_skills())
    except Exception:
        skill_count = 0
    try:
        mcp_count = len(mcp_client.get_active_clients())
    except Exception:
        mcp_count = 0

    b = branding.BannerInputs(
        model=config.MODEL,
        cwd=str(config.WORKSPACE),
        home=str(config.HOME),
        tool_count=tool_count,
        skill_count=skill_count,
        mcp_count=mcp_count,
    )

    parts = []
    for logo, title in branding.logo_with_titles(b):
        parts.append((logo, "magenta"))
        if title.strip().startswith("janus"):
            parts.append((title + "\n", "bold"))
        elif title:
            parts.append((title + "\n", "dim"))
        else:
            parts.append(("\n", ""))
    parts.append(("\n", ""))
    for line in branding.status_lines(b):
        parts.append((line + "\n", "dim"))

    console.print(Text.assemble(*parts))
    console.print(f"[dim]   {branding.COMMANDS_HINT}[/dim]\n")


def _show_interpretations(console, interps) -> None:
    """Boxed cards. Risk in the panel subtitle so it's adjacent to the label."""
    for i, x in enumerate(interps, 1):
        risk = (x.get("risk") or "—").strip()
        console.print(Panel(
            x.get("action", ""),
            title=f"[cyan]\\[{i}] [bold]{x.get('label', '')}[/bold][/cyan]",
            subtitle=f"[yellow]{risk}[/yellow]",
            subtitle_align="right",
            border_style="cyan",
            padding=(0, 1),
        ))


def _cmd_skills_rich(console, arg: str) -> bool:
    """`/skills` — list, filter, or install bundled.

    Usage:
      /skills                       list all installed skills
      /skills <query>               filter by name/description substring
      /skills install-bundled       copy janus/skills_bundled/ → ~/.janus/skills/
      /skills install-bundled --force   overwrite existing skill files
    """
    arg = (arg or "").strip()
    if arg.startswith("install-bundled"):
        rest = arg[len("install-bundled"):].strip()
        return _cmd_skills_install_bundled_rich(console, force=(rest == "--force"))
    items = skills.list_skills()
    if arg:
        items = skill_catalog.filter_skills(items, arg)
        if not items:
            console.print(f"[dim]no skills match '{arg}'[/dim]")
            return True
    elif not items:
        console.print(
            "[dim]no skills yet — try /skills install-bundled or /skill new[/dim]"
        )
        return True
    _show_skill_table(console, items)
    return True


def _cmd_skills_install_bundled_rich(console, *, force: bool = False) -> bool:
    result = skill_catalog.install_bundled(force=force)
    inst, skip, errs = result["installed"], result["skipped"], result["errors"]
    if not inst and not skip and not errs:
        console.print("[dim]no bundled skills to install[/dim]")
        return True
    if inst:
        console.print(
            f"[green]installed {len(inst)}[/]: {', '.join(inst)}"
        )
    if skip:
        console.print(
            f"[dim]skipped {len(skip)} (already installed): {', '.join(skip)}[/dim]"
        )
    if errs:
        console.print("[red]errors:[/]")
        for name, msg in errs:
            console.print(f"  [red]{name}: {msg}[/]")
    if inst:
        console.print(
            "[yellow]all installed skills are quarantined.[/] "
            "review with /skills, then /promote <name> trusted-supervised"
        )
    logger.write({
        "ts": logger.now_iso(),
        "type": "bundled_install",
        "installed": inst,
        "skipped": skip,
        "errors": [name for name, _ in errs],
    })
    return True


def _show_skill_table(console, items) -> None:
    if not items:
        console.print("[dim]no skills yet — try /skill new[/dim]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("name"); t.add_column("state"); t.add_column("description")
    t.add_column("runs"); t.add_column("trust")
    state_colors = {
        "quarantined": "yellow",
        "trusted-supervised": "cyan",
        "trusted-auto": "green",
    }
    for s in items:
        score = s.trust_score()
        score_label = (f"{int(score*100)}% {s.trust_label()}"
                       if score is not None else "—")
        t.add_row(
            s.name,
            f"[{state_colors.get(s.state,'white')}]{s.state}[/]",
            s.description[:60],
            f"{s.runs} ({s.success}/{s.fail})" if s.runs else "0",
            score_label,
        )
    console.print(t)


def _make_mode_approver(console, mode_state: permissions.ModeState):
    """v1.0 approver: consults the active permission mode + tool risk class.

    Tool risk arrives via the `risk=` kwarg the Registry injects (see
    tools/base.py). Capability tokens still short-circuit to True
    upstream via make_capability_aware() so a skill with explicit
    grants doesn't have to ask.

    Decision matrix per mode lives in permissions.decide().
    """
    def approver(action_label: str, details: str, **kw) -> bool:
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        decision = permissions.decide(risk, mode_state.current)

        if decision == permissions.ALLOW:
            return True
        if decision == permissions.DENY:
            console.print(
                f"[yellow]✗ {action_label}[/] "
                f"[dim]blocked by mode '{mode_state.current}' (risk={risk})[/]"
            )
            return False

        # ASK — show panel + y/N.
        console.print(Panel(
            details,
            title=f"[yellow]⚠ approval needed[/]: {action_label}  "
                  f"[dim](risk={risk}, mode={mode_state.current})[/]",
            border_style="yellow",
        ))
        try:
            ans = input("approve? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    return approver


# ---------- Slash command dispatcher ----------


def _dispatch(console, line: str, state: dict) -> bool:
    """Returns True if line was handled as a slash command."""
    if not line.startswith("/"):
        return False
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    # Phase 15: custom commands stash a rewritten request and return False
    # so the main loop processes it through the interpreter.
    custom = state.get("custom_commands") or {}
    custom_name = cmd[1:]
    if custom_name in custom:
        state["_pending_custom"] = custom[custom_name].render(arg)
        return False

    if cmd in ("/quit", "/exit"):
        state["quit"] = True
        return True
    if cmd == "/mode":
        return _cmd_mode(console, arg, state)
    if cmd == "/why":
        return _cmd_why(console, state)
    if cmd == "/help":
        cmds = _all_slash_commands(state.get("custom_commands"))
        t = Table(show_header=True, header_style="bold")
        t.add_column("category"); t.add_column("command"); t.add_column("description")
        cat_color = {"built-in": "cyan", "custom": "green"}
        for c in cmds:
            color = cat_color.get(c.category, "white")
            t.add_row(
                f"[{color}]● {c.category}[/]",
                c.name,
                c.description,
            )
        console.print(t)
        return True
    if cmd == "/workspace":
        if not arg:
            console.print(f"  current workspace: {config.WORKSPACE}")
        else:
            from pathlib import Path
            new = Path(arg).expanduser().resolve()
            if not new.exists() or not new.is_dir():
                console.print(f"[red]not a directory:[/] {new}")
            else:
                config.WORKSPACE = new
                console.print(f"  workspace → {new}")
        return True
    if cmd == "/analyze":
        from .cli import analyze
        analyze()
        return True
    if cmd == "/memory":
        target = arg.strip()
        if target:
            txt = memory.read(target)
            if not txt:
                console.print(f"[dim](no {target}.md yet)[/]")
            else:
                console.print(Markdown(txt))
            return True
        cats = memory.list_categories()
        configured = list(config.MEMORY_CATEGORIES)
        if not cats:
            console.print("[dim](no memory yet)[/]")
            console.print(
                f"[dim]categories ready to populate: "
                f"{', '.join(configured)}[/dim]"
            )
            return True
        for cat in cats:
            body = memory.read(cat).strip()
            console.print(
                f"[bold]{cat}.md[/] [dim]({len(body)} bytes)[/]"
            )
            console.print(Markdown(body))
            console.print()
        empty = [c for c in configured if c not in cats]
        if empty:
            console.print(
                f"[dim]empty: {', '.join(c + '.md' for c in empty)}[/dim]"
            )
        return True
    if cmd == "/search":
        if not arg.strip():
            console.print("[red]usage:[/] /search <query>")
            return True
        index.sync()
        hits = index.search(arg, k=10)
        if not hits:
            console.print("[dim]no matches.[/]")
            return True
        t = Table(show_header=True, header_style="bold")
        t.add_column("ts"); t.add_column("request"); t.add_column("tools"); t.add_column("output")
        for h in hits:
            t.add_row(h.ts[:19], h.request[:60], h.tools_used[:30],
                      (h.output.splitlines() or [""])[0][:60])
        console.print(t)
        return True
    if cmd == "/skills":
        return _cmd_skills_rich(console, arg)
    if cmd == "/swarm":
        from . import swarms as _swarms
        console.print(_swarms.slash.handle(arg))
        return True
    if cmd == "/promote":
        parts = arg.split()
        if len(parts) != 2:
            console.print("[red]usage:[/] /promote <name> <state>")
            return True
        try:
            s = skills.promote(parts[0], parts[1])
        except skills.PromotionError as e:
            console.print(f"[red]{e}[/]")
            return True
        console.print(f"  {s.name} → [green]{s.state}[/]")
        logger.write({"ts": logger.now_iso(), "type": "skill_promote",
                      "skill": s.name, "new_state": s.state})
        return True
    if cmd == "/skill":
        return _cmd_skill_new(console, arg)
    if cmd == "/eval":
        tokens = arg.split()
        last_n = config.EVAL_DEFAULT_LAST
        skill_filter: str | None = None
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--last":
                try:
                    last_n = int(tokens[i + 1]); i += 2; continue
                except (IndexError, ValueError):
                    console.print("[red]usage:[/] /eval [--last N] [--skill <name>]")
                    return True
            if tok == "--skill":
                try:
                    skill_filter = tokens[i + 1]; i += 2; continue
                except IndexError:
                    console.print("[red]usage:[/] /eval [--last N] [--skill <name>]")
                    return True
            i += 1
        suffix = f" (skill={skill_filter})" if skill_filter else ""
        console.print(f"[dim]replaying last {last_n} records at temp=0{suffix}...[/]")
        try:
            report = eval_mod.replay(last_n=last_n, skill_filter=skill_filter)
            console.print(eval_mod.render_summary(report))
        except Exception as e:
            console.print(f"[red]eval failed:[/] {e}")
        return True
    if cmd == "/mcp":
        return _cmd_mcp_rich(console, arg)
    if cmd == "/cost":
        for line in cost.render_summary().splitlines():
            console.print(line)
        return True
    if cmd == "/verbose":
        v = arg.strip().lower()
        if v in ("on", "true", "1"):
            state["verbose"] = True
            console.print("  [green]verbose mode ON[/]")
        elif v in ("off", "false", "0"):
            state["verbose"] = False
            console.print("  [yellow]verbose mode OFF[/]")
        else:
            console.print(f"  verbose mode: {'on' if state.get('verbose') else 'off'}")
        return True
    if cmd == "/init":
        console.print("[dim]scanning workspace + drafting starter user.md / skills…[/]")
        try:
            proposal = init_codebase.propose()
        except Exception as e:
            console.print(f"[red]/init failed:[/] {type(e).__name__}: {e}")
            return True
        if proposal.get("error"):
            console.print(f"[red]{proposal['error']}[/]")
            return True
        for ln in init_codebase.render(proposal).splitlines():
            console.print(ln)
        adds = proposal.get("user_md_additions") or []
        if adds:
            try:
                ans = input("apply user.md additions? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("y", "yes"):
                n = init_codebase.apply_user_md(adds)
                console.print(f"  [green]wrote {n} section(s) to user.md[/]")
        for sk in proposal.get("skill_proposals") or []:
            try:
                ans = input(
                    f"install skill '{sk.get('name', '?')}' (quarantined)? [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("y", "yes"):
                p = init_codebase.apply_skill(sk)
                console.print(f"  [green]wrote[/] {p}")
        return True
    if cmd == "/model":
        target = arg.strip()
        if not target:
            console.print(f"  current model: [bold]{config.MODEL}[/]")
            return True
        config.MODEL = target
        console.print(f"  [green]model -> {target}[/]  "
                      f"[dim](this session only; persist via JANUS_MODEL env)[/]")
        return True
    if cmd == "/doctor":
        console.print("[dim]running diagnostics…[/]\n")
        for ln in doctor.render(doctor.run_all(), color=False).splitlines():
            console.print(ln)
        return True
    if cmd == "/output-style" or cmd == "/style":
        target = arg.strip().lower()
        if not target:
            console.print(
                f"  current style: [bold]{state.get('output_style')}[/]  "
                f"[dim](valid: {', '.join(output_styles.VALID)})[/]"
            )
            return True
        if target not in output_styles.VALID:
            console.print(f"[red]unknown style:[/] {target}")
            return True
        state["output_style"] = target
        console.print(f"  [green]output style -> {target}[/]")
        return True
    if cmd == "/commands":
        customs = state.get("custom_commands") or {}
        if not customs:
            console.print(
                f"[dim]no custom commands. drop a .md at "
                f"{config.COMMANDS_DIR} to add one[/]"
            )
            return True
        t = Table(show_header=True, header_style="bold")
        t.add_column("name"); t.add_column("description"); t.add_column("path")
        for name, c in sorted(customs.items()):
            t.add_row(f"/{name}", c.description or "(none)", str(c.path))
        console.print(t)
        return True
    if cmd == "/stream":
        v = arg.strip().lower()
        if v in ("on", "true", "1"):
            state["stream"] = True
            console.print("  [green]streaming ON[/]")
        elif v in ("off", "false", "0"):
            state["stream"] = False
            console.print("  [yellow]streaming OFF[/]")
        else:
            console.print(f"  streaming: {'on' if state.get('stream', True) else 'off'}")
        return True
    if cmd == "/clear":
        conv = state.get("conv")
        if conv is not None:
            conv.clear_turns()
            try:
                conversation.save(conv)
            except Exception:
                pass
        cost.reset_session()
        console.print("  [green]cleared conversation turns + cost counters[/]")
        return True
    if cmd == "/compact":
        conv = state.get("conv")
        if conv is None or not conv.turns:
            console.print("[dim]nothing to compact (empty conversation)[/]")
            return True
        n_before = len(conv.turns)
        console.print(f"[dim]compacting {n_before} turns…[/]")
        try:
            conversation.compact(conv)
            conversation.save(conv)
        except Exception as e:
            console.print(f"[red]compact failed:[/] {type(e).__name__}: {e}")
            return True
        console.print(
            f"  [green]compacted {n_before - len(conv.turns)} turn(s)[/] -> "
            f"{len(conv.turns)} kept, {len(conv.summary)} char summary"
        )
        return True
    if cmd == "/resume":
        target = arg.strip()
        if not target:
            items = conversation.list_all()
            if not items:
                console.print("[dim]no saved conversations[/]")
                return True
            t = Table(show_header=True, header_style="bold")
            t.add_column("id"); t.add_column("turns"); t.add_column("last update")
            for item in items[:10]:
                t.add_row(item["id"], str(item["turns"]),
                          item["last_updated"][:19])
            console.print(t)
            console.print("[dim]usage: /resume <id>[/]")
            return True
        conv = conversation.load(target)
        if conv is None:
            console.print(f"[red]no conversation '{target}'[/]")
            return True
        state["conv"] = conv
        console.print(f"  [green]resumed[/] {conv.id} "
                      f"({len(conv.turns)} turns, started {conv.started[:19]})")
        return True
    if cmd == "/continue":
        latest = conversation.latest()
        if latest is None:
            console.print("[dim]no prior conversation to continue[/]")
            return True
        state["conv"] = latest
        console.print(f"  [green]continuing[/] {latest.id} "
                      f"({len(latest.turns)} turns)")
        return True
    if cmd == "/triggers":
        from . import triggers as trg
        items = trg.list_triggers()
        if not items:
            console.print("[dim]no triggers — drop a YAML file in[/] " + str(config.TRIGGERS_DIR))
            return True
        t = Table(show_header=True, header_style="bold")
        t.add_column("name"); t.add_column("kind"); t.add_column("when")
        t.add_column("skill"); t.add_column("last fired")
        for tr in items:
            t.add_row(tr.name, tr.kind, tr.when, tr.skill or "-",
                      tr.last_fired or "-")
        console.print(t)
        return True

    console.print(f"[red]unknown command:[/] {cmd}")
    return True


def _cmd_mode(console, arg: str, state: dict) -> bool:
    """`/mode [name]` — show or switch the active permission mode."""
    mode_state: permissions.ModeState = state["mode_state"]
    target = arg.strip()
    if not target:
        # Show current + list options.
        rows = [
            (permissions.DEFAULT, "read auto · write/exec ask"),
            (permissions.ACCEPT_EDITS, "read+write auto · exec ask"),
            (permissions.PLAN, "read auto · write/exec DENY"),
            (permissions.AUTO, "everything auto BUT risky calls blocked (rm -rf /, SSRF, ...)"),
            (permissions.BYPASS, "everything auto · no prompts (no safety net)"),
        ]
        t = Table(show_header=True, header_style="bold")
        t.add_column("mode"); t.add_column("behavior")
        for name, desc in rows:
            marker = "● " if name == mode_state.current else "  "
            color = "magenta" if name == mode_state.current else "white"
            t.add_row(f"[{color}]{marker}{name}[/]", desc)
        console.print(t)
        console.print(
            f"[dim]usage: /mode <name>  ·  current: "
            f"[bold]{mode_state.current}[/][/dim]"
        )
        return True
    if target not in permissions.ALL_MODES:
        # Try legacy name normalization (manual/auto/dry-run).
        normalized = permissions.normalize(target)
        if normalized != permissions.DEFAULT or target.lower() in (
            "manual", "auto", "dry-run", "default"
        ):
            target = normalized
        else:
            console.print(
                f"[red]unknown mode:[/] {target}  "
                f"[dim]valid: {', '.join(permissions.ALL_MODES)}[/]"
            )
            return True
    mode_state.set(target)
    if target == permissions.BYPASS:
        color = "red"
    elif target == permissions.AUTO:
        color = "magenta"
    else:
        color = "green"
    console.print(f"  [{color}]mode → {mode_state.current}[/]")
    if target == permissions.BYPASS:
        console.print(
            "  [red]warning:[/] every tool will run without asking. "
            "[dim]/mode default to disable.[/]"
        )
    elif target == permissions.AUTO:
        console.print(
            "  [dim]auto mode: tools auto-approve but rm -rf /, "
            "fs writes to /etc/, SSRF fetches, etc. are blocked. "
            "Add patterns at ~/.janus/auto_risk_patterns.yaml.[/dim]"
        )
    logger.write({
        "ts": logger.now_iso(),
        "type": "mode_switch",
        "new_mode": target,
    })
    return True


def _cmd_why(console, state: dict) -> bool:
    """`/why` — re-run the last user input through the legacy interpreter
    and show 2-3 candidate readings. Power-user escape hatch for users
    who want to inspect what alternatives the model considered."""
    last = state.get("last_user_input", "")
    if not last:
        console.print("[dim]nothing to interpret yet — type a message first[/]")
        return True
    console.print(f"[dim]re-interpreting:[/] {last[:80]}{'…' if len(last) > 80 else ''}")
    conv = state.get("conv")
    cache_snap = cache.snapshot()
    preamble = cache_snap.preamble + (conv.recent_context_block() if conv else "")
    try:
        interps = interpreter.interpret(
            last,
            memory_preamble=preamble,
            tool_count=len(default_registry().names()),
            skill_count=len(skills.list_skills()),
        )
    except Exception as e:
        console.print(f"[red]interpreter failed:[/] {e}")
        return True
    if not interps:
        console.print("[dim](no interpretations returned)[/]")
        return True
    _show_interpretations(console, interps)
    console.print(
        "[dim]This is read-only — none of these were executed. "
        "Send a new message to act.[/]"
    )
    return True


def _cmd_skill_new(console, arg: str) -> bool:
    parts = arg.strip().split(maxsplit=1)
    if not parts:
        console.print("[red]usage:[/] /skill new | /skill review <name> | /skill import <source>")
        return True
    sub = parts[0]
    if sub == "new":
        return _cmd_skill_draft(console)
    if sub == "review":
        if len(parts) < 2 or not parts[1].strip():
            console.print("[red]usage:[/] /skill review <name>")
            return True
        return _cmd_skill_review(console, parts[1].strip())
    if sub == "import":
        if len(parts) < 2 or not parts[1].strip():
            console.print("[red]usage:[/] /skill import <path-or-url>")
            return True
        return _cmd_skill_import(console, parts[1].strip())
    console.print("[red]usage:[/] /skill new | /skill review <name> | /skill import <source>")
    return True


def _cmd_skill_import(console, source: str) -> bool:
    console.print(f"[dim]importing skill from {source}…[/]")
    try:
        path = skills_market.import_skill(source)
    except Exception as e:
        console.print(f"[red]import failed:[/] {type(e).__name__}: {e}")
        return True
    console.print(f"  [green]imported[/] -> {path}")
    name = path.stem if path.suffix == ".md" else path.parent.name
    console.print(
        f"  [yellow]skill is quarantined.[/] review with /skills, then "
        f"/promote {name} trusted-supervised"
    )
    # Phase 18: surface what changed vs the closest installed skill.
    try:
        neighbor = skills_market.diff_against_neighbor(path)
    except Exception:
        neighbor = None
    if neighbor:
        console.print()
        console.print("[dim]--- diff vs closest installed skill ---[/]")
        for line in neighbor.splitlines():
            console.print(line)
    logger.write({
        "ts": logger.now_iso(),
        "type": "skill_import",
        "source": source,
        "path": str(path),
    })
    return True


def _cmd_mcp_rich(console, arg: str) -> bool:
    parts = arg.strip().split(maxsplit=1)
    sub = parts[0] if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub == "list":
        servers = mcp_client.load_servers()
        active = mcp_client.get_active_clients()
        if not servers and not active:
            console.print(
                f"[dim]no MCP servers configured. drop a JSON config at "
                f"{config.MCP_SERVERS_FILE} or use ~/.claude/settings.json[/]"
            )
            return True
        t = Table(show_header=True, header_style="bold")
        t.add_column("name"); t.add_column("status"); t.add_column("command"); t.add_column("args")
        for name, cfg in servers.items():
            status = "connected" if name in active else "configured"
            color = "green" if name in active else "dim"
            t.add_row(name, f"[{color}]{status}[/]", cfg.command, " ".join(cfg.args))
        for name in active:
            if name not in servers:
                t.add_row(name, "[green]connected[/]", "(not in config)", "")
        console.print(t)
        return True
    if sub == "connect":
        if not rest:
            console.print("[red]usage:[/] /mcp connect <server>")
            return True
        servers = mcp_client.load_servers()
        cfg = servers.get(rest)
        if cfg is None:
            console.print(f"[red]no MCP server '{rest}' in config[/]")
            return True
        console.print(f"[dim]spawning '{rest}' ({cfg.command} {' '.join(cfg.args)})…[/]")
        try:
            client = mcp_client.connect_server(cfg)
            tools = client.list_tools()
        except Exception as e:
            console.print(f"[red]connect failed:[/] {type(e).__name__}: {e}")
            return True
        mcp_client.register_client(rest, client)
        console.print(
            f"  [green]connected[/] '{rest}' — {len(tools)} tool(s) mounted as mcp_{rest}_*"
        )
        for tdef in tools:
            console.print(f"    - {tdef.get('name', '?')}")
        logger.write({
            "ts": logger.now_iso(),
            "type": "mcp_connect",
            "server": rest,
            "tool_count": len(tools),
        })
        return True
    if sub == "disconnect":
        if not rest:
            console.print("[red]usage:[/] /mcp disconnect <server>")
            return True
        if mcp_client.unregister_client(rest):
            console.print(f"  [green]disconnected[/] '{rest}'")
            logger.write({"ts": logger.now_iso(), "type": "mcp_disconnect", "server": rest})
        else:
            console.print(f"  [yellow]server '{rest}' was not connected[/]")
        return True
    console.print("[red]usage:[/] /mcp list | /mcp connect <server> | /mcp disconnect <server>")
    return True


def _cmd_skill_draft(console) -> bool:
    try:
        pattern = input("what pattern do you want to capture? ").strip()
    except (EOFError, KeyboardInterrupt):
        return True
    if not pattern:
        return True
    recent = logger.read_all()[-20:]
    console.print(f"[dim]drafting against last {len(recent)} log entries…[/]")
    try:
        draft = skills.draft_skill_from_log(pattern, recent)
    except Exception as e:
        console.print(f"[red]draft failed:[/] {e}")
        return True
    console.print(Panel(
        f"[bold]name[/]: {draft.get('name')}\n"
        f"[bold]description[/]: {draft.get('description')}\n"
        f"[bold]capabilities[/]: {draft.get('capabilities')}\n"
        f"[bold]body[/]:\n{(draft.get('body') or '')[:2000]}",
        title="draft", border_style="cyan",
    ))
    try:
        ans = input("save as quarantined skill? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return True
    if ans in ("y", "yes"):
        path = skills.write_draft(draft)
        console.print(f"  [green]wrote[/] {path}")
    return True


def _cmd_skill_review(console, name: str) -> bool:
    skill = skills.load(name)
    if skill is None:
        console.print(f"[red]no skill named '{name}'[/]")
        return True
    console.print(
        f"[dim]reviewing skill '{name}' (runs={skill.runs}, "
        f"success={skill.success}, fail={skill.fail})…[/]"
    )
    try:
        revision = skill_evolution.propose_revision(skill)
    except Exception as e:
        console.print(f"[red]propose failed:[/] {type(e).__name__}: {e}")
        return True
    console.print(Panel(
        skill_evolution.render_revision(skill, revision),
        title=f"revision proposal for {name}", border_style="cyan",
    ))
    if not revision.get("changed"):
        return True
    try:
        ans = input("apply this revision? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return True
    if ans in ("y", "yes"):
        skill_evolution.apply_revision(skill, revision)
        logger.write({
            "ts": logger.now_iso(),
            "type": "skill_revision_applied",
            "skill": skill.name,
            "rationale": revision.get("rationale", ""),
        })
        console.print(f"  [green]applied[/] → {skill.path}")
    return True


# ---------- Memory propose hook ----------


def _maybe_propose_memory(console, req: str, output: str,
                          cache_snap=None) -> None:
    if not config.MEMORY_PROPOSE_ENABLED:
        return
    try:
        ops = memory.propose_diff(req, output)
    except Exception as e:
        console.print(f"[dim]memory propose skipped: {type(e).__name__}: {e}[/]")
        return
    if not ops:
        return
    console.print(Panel(memory.render_diff(ops), title="proposed memory updates",
                        border_style="cyan"))
    try:
        ans = input("apply? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans in ("y", "yes"):
        memory.apply(ops)
        console.print("  [green]applied to user.md[/]")
        if cache_snap is not None:
            cache_snap.preamble = cache.snapshot().preamble


# ---------- Main loop ----------


def main() -> None:
    """v1.0 main loop — Claude-Code-shaped chat with mode-gated tool use.

    No more interpretation picker. The user types a message, the model
    streams a response, tools fire inline (with permission gates per
    mode), and the next turn picks up where this one ended. Slash
    commands handle meta-actions; `/why` exposes the old interpreter
    flow on demand for users who want to inspect ambiguity.
    """
    _need_libs()
    config.assert_configured()
    config.ensure_home()
    console = Console()
    _banner(console)

    try:
        added = index.sync()
        if added:
            console.print(f"[dim]indexed {added} new log entries[/]")
    except Exception as e:
        console.print(f"[dim]index sync skipped: {e}[/]")

    # v1.0 mode state. Seeded from legacy JANUS_APPROVAL for back-compat.
    mode_state = permissions.ModeState(
        current=permissions.normalize(config.APPROVAL_MODE)
    )
    if mode_state.current == permissions.BYPASS:
        console.print(
            "[red bold]⚠ bypassPermissions mode active —[/] "
            "[red]every tool will run without asking. /mode default to disable.[/]"
        )

    bindings = KeyBindings()
    base_approver = _make_mode_approver(console, mode_state)
    state: dict[str, Any] = {
        "quit": False,
        "conv": None,
        "verbose": False,
        "turn": 0,
        "stream": True,
        "output_style": config.OUTPUT_STYLE,
        "custom_commands": {},
        "mode_state": mode_state,
        # v1.0: full conversation message list, persisted across turns
        # so the model sees prior context. system message lives at index 0
        # and is rebuilt by executor.chat() each turn.
        "messages": [],
        # Last user input — used by /why to re-interpret on demand.
        "last_user_input": "",
    }
    history = FileHistory(str(config.HISTORY_FILE))
    session = PromptSession(
        history=history,
        completer=SlashCompleter(lambda: state.get("custom_commands") or {}),
        style=JANUS_STYLE,
        reserve_space_for_menu=12,
    )

    cache_snap = cache.snapshot()
    pending = conversation.take_pending()
    state["conv"] = pending if pending is not None else conversation.new()
    if pending is not None:
        console.print(
            f"[dim]   resumed conversation {state['conv'].id} "
            f"({len(state['conv'].turns)} turns)[/]\n"
        )
        # Rebuild messages from saved turns so the model has prior context.
        # We only restore user/assistant text — tool calls and tool results
        # were ephemeral to the original turn.
        for t in state["conv"].turns:
            req = (t.get("request") or "").strip()
            out = (t.get("output") or "").strip()
            if req:
                state["messages"].append({"role": "user", "content": req})
            if out:
                state["messages"].append({"role": "assistant", "content": out})

    try:
        state["custom_commands"] = commands_mod.load_all()
        n = len(state["custom_commands"])
        if n:
            console.print(f"[dim]   loaded {n} custom command(s)[/]\n")
    except Exception:
        state["custom_commands"] = {}

    prompt_text = FormattedText([("bold ansigreen", f" {branding.PROMPT_GLYPH}  ")])
    cont_text = FormattedText([("ansigray", "   …  ")])

    while not state["quit"]:
        st = statusline.render(statusline.StatusInputs(
            model=config.MODEL,
            turn=state.get("turn", 0),
            plan_on=False,
            parallel_on=False,
            verbose=state.get("verbose", False),
            permission_mode=mode_state.current,
            conv_turns=len(state["conv"].turns) if state.get("conv") else 0,
        ))
        console.print(f"[dim]{st}[/]")
        try:
            line = session.prompt(prompt_text)
            collected = []
            while line.endswith("\\") and not line.endswith("\\\\"):
                collected.append(line[:-1])
                cont = session.prompt(cont_text)
                if not cont:
                    line = ""; break
                line = cont
            collected.append(line)
            req = "\n".join(collected).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not req:
            continue
        if req.lower() in ("q", "quit", "exit"):
            break
        if _dispatch(console, req, state):
            continue
        if state.get("_pending_custom"):
            req = state.pop("_pending_custom")

        state["last_user_input"] = req

        record: dict[str, Any] = {
            "ts": logger.now_iso(),
            "model": config.MODEL,
            "workspace": str(config.WORKSPACE),
            "request": req,
            "mode": mode_state.current,
        }

        cost.new_turn()
        state["turn"] = state.get("turn", 0) + 1

        conv = state["conv"]
        preamble = cache_snap.preamble + conv.recent_context_block()

        # Skill auto-attach: trusted-auto matches only. Non-auto matches
        # are surfaced as a hint but never block the turn.
        all_skills = skills.list_skills()
        matches = skills.match(req, all_skills)
        attached_skill = None
        for s in matches:
            if s.state == "trusted-auto":
                attached_skill = s
                console.print(f"[dim]auto-attached skill:[/] {s.name}")
                break
        if attached_skill:
            record["skill"] = attached_skill.name
            record["skill_state"] = attached_skill.state
        elif matches:
            names = ", ".join(s.name for s in matches[:3])
            console.print(
                f"[dim]matching skills (not auto):[/] {names} "
                f"[dim](promote one to attach automatically)[/]"
            )

        caps = attached_skill.capabilities if attached_skill else CapabilitySet()
        tools = default_registry(capabilities=caps)
        approver = make_protected(base_approver, caps, mode_state.current)

        try:
            t0 = time.time()
            step_renderer = _render_step_factory(console, state)
            output, trace = executor.chat(
                messages=state["messages"],
                user_input=req,
                tools=tools,
                approver=approver,
                on_step=step_renderer,
                skill_body=(attached_skill.body if attached_skill else ""),
                memory_preamble=preamble,
                mode=mode_state.current,
                workspace=str(config.WORKSPACE),
                tool_count=len(tools.names()),
                skill_count=len(all_skills),
                stream=state.get("stream", True),
            )
            _flush_stream(console, state)
            record["execute_ms"] = int((time.time() - t0) * 1000)
            record["trace"] = trace
            record["output"] = output
        except Exception as e:
            console.print(f"[red]executor failed:[/] {e}")
            record["error"] = f"execute: {e}"
            logger.write(record)
            continue

        # Render any final text the model produced. Stream already wrote
        # most of it; this is the markdown re-render for fenced code blocks
        # etc. only when a long markdown-shaped reply came back without
        # streaming, or when the user wants a different output style.
        rendered = output_styles.render(output, state.get("output_style", "markdown"))
        if not state.get("stream", True) or not output:
            # Streaming already painted it; only print again when we didn't.
            if rendered.strip():
                console.print()
                console.print(
                    Markdown(rendered)
                    if state.get("output_style") == "markdown"
                       and "\n" in rendered and len(rendered) > 80
                    else rendered
                )

        if attached_skill:
            success = skill_evolution.resolve_success(output, trace, None)
            try:
                updated = skills.record_run(attached_skill.name, success=success)
            except Exception:
                updated = None
            if updated and skill_evolution.should_propose(updated):
                console.print(
                    f"[dim]skill '{updated.name}' has {updated.runs} runs "
                    f"(success={updated.success}, fail={updated.fail}); "
                    f"consider /skill review {updated.name}[/]"
                )
        logger.write(record)

        try:
            conv.add_turn(
                request=req, output=output,
                choice="chat",
                skill=(attached_skill.name if attached_skill else None),
                ts=record.get("ts"),
            )
            conversation.save(conv)
        except Exception:
            pass

        if len(conv.turns) >= config.COMPACT_THRESHOLD_TURNS:
            console.print(
                f"[dim]({len(conv.turns)} turns this conversation; "
                f"consider /compact)[/]"
            )

        try:
            index.sync()
        except Exception:
            pass

        _maybe_propose_memory(console, req, output, cache_snap=cache_snap)

    console.print(f"[dim]bye.[/]")


def _render_step_factory(console, state=None):
    """state is the cli_rich main-loop state dict; we read `verbose` and
    use `_stream_buffer` to flush stream chunks correctly."""
    state = state or {}
    state.setdefault("_stream_buffer", "")

    def render_step(step: dict) -> None:
        if step["type"] == "tool_call":
            verbose = state.get("verbose", False)
            limit = 200 if verbose else 60
            args_brief = ", ".join(
                f"{k}={(str(v) if len(str(v)) < limit else str(v)[:limit-3] + '…')}"
                for k, v in step["args"].items()
            )
            # Flush any in-flight stream chunk first so it doesn't get
            # interleaved with the tool call line.
            _flush_stream(console, state)
            console.print(
                f"[dim]   {branding.TOOL_CALL_ARROW} {step['tool']}({args_brief})[/]"
            )
        elif step["type"] == "tool_result":
            preview = step.get("result_preview", "")
            head = preview.splitlines()[0][:80] if preview else ""
            if head:
                console.print(f"[dim]    {branding.TOOL_OK} {head}[/]")
        elif step["type"] == "stream_chunk":
            text = step.get("text", "")
            if text:
                # Use raw stdout write for unbuffered streaming feel; rich
                # would re-render the whole panel for each chunk.
                console.file.write(text)
                console.file.flush()
                state["_stream_buffer"] = state.get("_stream_buffer", "") + text
        elif step["type"] == "final":
            _flush_stream(console, state)
    return render_step


def _flush_stream(console, state):
    if state.get("_stream_buffer"):
        console.file.write("\n")
        console.file.flush()
        state["_stream_buffer"] = ""


def _serialize_plan(node) -> dict:
    return {
        "id": node.id, "goal": node.goal, "skill": node.skill,
        "deps": list(node.deps),
        "children": [_serialize_plan(c) for c in node.children],
    }
