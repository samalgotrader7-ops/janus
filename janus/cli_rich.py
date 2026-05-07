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
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import app, config, interpreter, executor, logger, memory, index, skills
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


# v1.24.0: SlashCommand and BUILTIN_COMMANDS moved to slash_dispatch.py
# (single source of truth across cli_rich / cli / tui). Re-exported here
# for back-compat with anything that still imports from cli_rich.
from .slash_dispatch import (
    SlashCommand,
    BUILTIN_COMMANDS,
    SLASH_COMMANDS,
    all_slash_commands as _shared_all_slash_commands,
)


_CATEGORY_DOT = {
    "built-in": "ansicyan",
    "custom":   "ansigreen",
}
_CATEGORY_ORDER = {"built-in": 0, "custom": 1}


def _all_slash_commands(customs: dict | None) -> list[SlashCommand]:
    """Built-ins + customs, sorted by (category, name) for stable grouping.

    v1.24.0: thin wrapper around slash_dispatch.all_slash_commands so
    cli_rich's dropdown stays untouched. Custom command rendering
    differs from the shared helper (cli_rich uses cc.description as an
    attribute, not key); we delegate when the shape matches.
    """
    return _shared_all_slash_commands(customs)


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
        """Autocomplete slash commands AND `@path` file mentions.

        `customs_provider` is a zero-arg callable returning the current
        custom-commands dict. We keep it as a callable (not a snapshot) so
        the dropdown reflects state changes — e.g. a future /reload that
        re-scans `~/.janus/commands/` mid-session works without rebuilding
        the completer.

        v1.25.1: also handles `@<path>` for inlining workspace files
        into the next user turn. The completer matches the @-token at
        the cursor; submission post-processing (in the chat loop) does
        the actual file inlining via at_mentions.expand_at_mentions.
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
                return
            # v1.25.1: `@path` file mentions. Look back from cursor for
            # an @-token preceded by start-of-string or whitespace.
            yield from _at_completions(text)


    def _at_completions(text: str):
        """Yield workspace-file completions when text-before-cursor is
        a partial @-mention. Matches the same `@<path>` shape as the
        post-submit expansion in at_mentions.py.

        Cursor must be inside an @-token; otherwise yields nothing.
        """
        # Find the last `@` in text. To be a valid mention it must be
        # at start-of-string or preceded by whitespace, and there must
        # be no whitespace between it and the cursor.
        at_pos = text.rfind("@")
        if at_pos < 0:
            return
        # Anything between @ and cursor must be path-shaped (no spaces).
        partial = text[at_pos + 1:]
        if any(c.isspace() for c in partial):
            return
        # Must be at start or preceded by whitespace.
        if at_pos > 0 and not text[at_pos - 1].isspace():
            return
        from . import at_mentions as _am
        try:
            matches = _am.list_workspace_files(prefix=partial, max_results=20)
        except Exception:
            return
        for relpath in matches:
            is_dir = relpath.endswith("/")
            display = FormattedText([
                ("ansiblue" if is_dir else "ansigreen", "📁 " if is_dir else "  "),
                ("", relpath),
            ])
            yield Completion(
                relpath,
                start_position=-len(partial),
                display=display,
                display_meta="  " + ("directory" if is_dir else "file"),
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


def _cmd_memory_about_me(console) -> bool:
    """`/memory about-me` — Janus reads back its current understanding.

    v1.19.0 Phase 6. Renders cards grouped by category + legacy .md
    snippets so the user can verify accuracy. Corrections flow through
    the normal propose_diff path on the next chat turn.
    """
    from . import interviews, memory, memory_cards, memory_index
    from pathlib import Path
    try:
        memory_index.reconcile()
    except Exception:
        pass

    rows = memory_index.list_all()
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)

    console.print("[bold]Here's what I know about you:[/]")

    any_cards = False
    for cat in interviews.SUPPORTED_CATEGORIES:
        cat_rows = by_type.get(cat, [])
        if not cat_rows:
            console.print(f"\n[bold]{cat}[/] [dim italic](no cards yet)[/]")
            continue
        any_cards = True
        console.print(f"\n[bold]{cat}[/]")
        for r in cat_rows[:10]:
            try:
                card = memory_cards.read_card(Path(r["path"]))
                content = card.content[:200].replace("\n", " ")
                console.print(f"  • [cyan]{r['subject']}:[/] {content}")
            except Exception:
                continue

    # Legacy categories — show only non-empty ones, briefly.
    legacy_shown = False
    for cat in ("soul", "user", "project", "preferences", "relationships"):
        body = memory.read(cat).strip() if hasattr(memory, "read") else ""
        if not body:
            continue
        if not legacy_shown:
            console.print("\n[dim]Legacy .md notes (curated by you):[/]")
            legacy_shown = True
        console.print(f"\n  [bold dim]{cat}.md:[/]")
        for line in body.splitlines()[:6]:
            console.print(f"    [dim]{line}[/]")

    if not any_cards and not legacy_shown:
        console.print(
            "\n[dim](nothing yet — try `/interview` to fill in your profile)[/]"
        )
        return True

    console.print(
        "\n[dim italic]anything wrong? reply with corrections — "
        "I'll update memory.[/]"
    )
    return True


def _cmd_interview_rich(console, arg: str) -> bool:
    """`/interview [<category>|daily]` — populate memory cards Q&A-style.

    v1.19.0 Phases 3+4. No arg → walk all 8 categories. With a category
    name → walk just that one. With ``daily`` → enable drip mode (Phase 4).
    """
    from . import interviews, interview_runner
    arg = (arg or "").strip().lower()

    # Auto-install bundled library if missing (Phase 8 will move this
    # to cache.snapshot()).
    interviews.maybe_install_bundled()

    # Drip subcommand handled in Phase 4 wire-in.
    if arg == "daily":
        return _cmd_interview_drip_rich(console, "")
    if arg.startswith("daily "):
        return _cmd_interview_drip_rich(console, arg[6:])
    if arg in ("pause", "stop"):
        state = interviews.load_state("cli", "default")
        state.mode = "idle"
        interviews.save_state(state)
        console.print("[yellow]interview paused[/]")
        return True

    category_filter: str | None = None
    if arg and arg in interviews.SUPPORTED_CATEGORIES:
        category_filter = arg
    elif arg:
        console.print(
            f"[red]usage:[/] /interview [<category>|daily|pause]\n"
            f"  category: {', '.join(interviews.SUPPORTED_CATEGORIES)}"
        )
        return True

    state = interviews.load_state("cli", "default")
    library = interviews.load_all()
    if not library:
        console.print(
            "[yellow]interview library is empty[/] — "
            "rerun `/interview` after restart to auto-install bundled questions."
        )
        return True

    # Print header
    if category_filter:
        cat = library.get(category_filter)
        if cat is None:
            console.print(
                f"[red]category {category_filter!r} not in library[/]"
            )
            return True
        console.print(
            f"\n[bold]{category_filter}[/] — {cat.description}"
        )
    else:
        console.print(
            "\n[bold]Memory interview[/] — let's fill in your profile.\n"
            "[dim]Type your answer, 'skip' to skip a question, "
            "'later' to pause.[/]"
        )

    def _ask(question, fqid: str) -> str:
        console.print(f"\n[bold cyan]?[/] {question.question}")
        if question.placeholder:
            console.print(f"  [dim]e.g. {question.placeholder}[/]")
        if question.mode == "choices" and question.choices:
            for i, c in enumerate(question.choices, 1):
                console.print(f"  [dim]{i}.[/] {c}")
            console.print(
                "  [dim](number, free text, 'skip', or 'later')[/]"
            )
        else:
            console.print("  [dim]('skip' or 'later' to skip / pause)[/]")
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return interview_runner.LATER_TOKEN
        low = raw.lower()
        if low in ("skip", "/skip"):
            return interview_runner.SKIP_TOKEN
        if low in ("later", "/later", "/cancel"):
            return interview_runner.LATER_TOKEN
        if question.mode == "choices" and raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(question.choices):
                return question.choices[idx]
        return raw

    result = interview_runner.run_one_shot(
        state, library, _ask,
        category_filter=category_filter,
        scope="global",
    )

    # Summary
    console.print()
    console.print(
        f"[green]✓ {result.answered} answered, "
        f"{result.skipped} skipped, "
        f"{len(result.cards_written)} cards written[/]"
    )
    if result.cancelled:
        console.print("[dim](paused — resume with /interview)[/]")

    # Profile completion meter
    console.print("\n[bold]Profile completion[/]")
    for line in interview_runner.render_completion_meter(
        result.completion_pct,
    ):
        console.print(line)
    return True


def _cmd_interview_drip_rich(console, arg: str) -> bool:
    """`/interview daily [N]` — turn on drip mode for this gateway.

    v1.19.0 Phase 4. Sets state.mode='drip' so each subsequent chat
    turn prepends ONE question via the gateway hook.
    """
    from . import interviews
    interviews.maybe_install_bundled()
    state = interviews.load_state("cli", "default")
    try:
        per_day = int(arg.strip()) if arg.strip() else interviews.DRIP_DEFAULT_PER_DAY
    except ValueError:
        per_day = interviews.DRIP_DEFAULT_PER_DAY
    per_day = max(1, min(10, per_day))
    state.mode = "drip"
    if not state.started_at:
        state.started_at = interviews._now_iso()
    interviews.reset_drip_quota(state, per_day=per_day)
    interviews.save_state(state)
    console.print(
        f"[green]drip mode on[/] — Janus will ask up to {per_day} "
        f"question(s) per day on this surface. "
        f"Auto-pauses at {int(interviews.DRIP_AUTO_PAUSE_PCT*100)}% "
        f"profile completion. Resume manually with `/interview daily`."
    )
    return True


def _cmd_skills_suggestions(console, state: dict) -> bool:
    """v1.28.0 — `/skills suggestions`: list recurring patterns the
    skill_proposer detected over the current session + recent log.

    Patterns in cooldown / already accepted are filtered out.
    """
    from . import skill_proposer
    conv = state.get("conv")
    current_trace = (
        (conv.turns[-1].get("trace") if conv and conv.turns else None)
        or state.get("last_trace")
    )
    patterns = skill_proposer.list_offerable(current_trace=current_trace)
    if not patterns:
        console.print(
            "[dim]no recurring patterns detected yet — keep working "
            "and Janus will surface suggestions as they emerge[/]"
        )
        return True
    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("id", overflow="fold")
    t.add_column("kind")
    t.add_column("hits", justify="right", width=4)
    t.add_column("description", overflow="fold")
    for p in patterns[:10]:
        t.add_row(p.id, p.kind, str(p.occurrences), p.description)
        skill_proposer.mark_offered(p.id)
    console.print(t)
    console.print(
        "[dim]  /skills propose <id>  → LLM-draft a skill (writes to "
        "skills dir, state=quarantined)[/]\n"
        "[dim]  /skills decline <id>  → silence for "
        f"{skill_proposer.COOLDOWN_DAYS} days[/]"
    )
    return True


def _cmd_skills_propose(console, state: dict, pattern_id: str) -> bool:
    """v1.28.0 — `/skills propose <id>`: LLM-draft a skill from a
    detected pattern. The draft lands as a quarantined skill the user
    can /promote after review."""
    from . import skill_proposer
    pattern_id = (pattern_id or "").strip()
    if not pattern_id:
        console.print("[red]usage:[/] /skills propose <pattern-id>")
        return True
    conv = state.get("conv")
    current_trace = (
        (conv.turns[-1].get("trace") if conv and conv.turns else None)
        or state.get("last_trace")
    )
    patterns = skill_proposer.detect(current_trace=current_trace)
    target = next((p for p in patterns if p.id == pattern_id), None)
    if target is None:
        console.print(
            f"[red]no pattern with id[/] [bold]{pattern_id}[/] "
            "[dim](run /skills suggestions to see current ids)[/]"
        )
        return True
    console.print(
        f"[dim]drafting skill for pattern[/] [cyan]{target.id}[/] "
        "[dim](one LLM call)…[/]"
    )
    try:
        path = skill_proposer.draft_skill(
            target, current_trace=current_trace,
        )
    except Exception as e:
        console.print(f"[red]draft failed:[/] {type(e).__name__}: {e}")
        return True
    console.print(
        f"  [green]drafted[/] {path.name} "
        "[dim](state=quarantined; review and /promote when ready)[/]"
    )
    return True


def _cmd_skills_decline(console, pattern_id: str) -> bool:
    """v1.28.0 — `/skills decline <id>`: silence a pattern offer for
    the cooldown window."""
    from . import skill_proposer
    pattern_id = (pattern_id or "").strip()
    if not pattern_id:
        console.print("[red]usage:[/] /skills decline <pattern-id>")
        return True
    skill_proposer.mark_declined(pattern_id)
    console.print(
        f"  [dim]declined[/] {pattern_id} "
        f"[dim](silenced for {skill_proposer.COOLDOWN_DAYS} days)[/]"
    )
    return True


def _cmd_skills_rich(console, arg: str) -> bool:
    """`/skills` — list, filter, or install bundled.

    Usage:
      /skills                       list all installed skills
      /skills <query>               filter by name/description substring
      /skills install-bundled       copy janus/skills_bundled/ → ~/.janus/skills/
      /skills install-bundled --force   overwrite existing skill files
      /skills suggestions           v1.28.0: list detected recurring patterns
      /skills propose <id>          v1.28.0: LLM-draft a skill for a pattern
      /skills decline <id>          v1.28.0: silence a pattern for cooldown
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


def _is_high_risk_grant(tool_name: str, capability) -> tuple[bool, str]:
    """v1.24.6 #5: decide whether the [s]ession/[a]lways grant options
    should be SUPPRESSED for this approval prompt.

    A "high-risk grant" is one where granting the (tool, risk) pair
    blanket-approves a class of action whose target the grant doesn't
    constrain — e.g. `[a]lways approve fs_write` would auto-approve
    EVERY future fs_write, including writes to docs/, .github/,
    LICENSE, etc. Sam's 2026-05-07 session: he was offered
    [a]lways for the docs/SWARM_EXPLAINER.md write; one fat-finger
    of `a` and Janus would silently overwrite docs/ forever.

    Returns (is_high_risk, reason). Reason renders in the prompt
    so the user understands why the options shrank.

    Triggers:
      * fs_write/fs_edit/fs_multi_edit on a protected path
        (docs/, .github/, vendor/, node_modules/, LICENSE, CHANGELOG.md)
      * shell/ssh_exec with a regret-pattern command
        (git push --force, terraform destroy, kubectl delete,
         rm -rf $/~/, raw block-device write, etc.)
    """
    target = ""
    if isinstance(capability, (tuple, list)) and len(capability) >= 3:
        target = str(capability[2] or "")

    if tool_name in ("fs_write", "fs_edit", "fs_multi_edit") and target:
        try:
            from . import tool_guardrails
            from pathlib import Path as _P
            path_obj = _P(target).expanduser()
            for matcher, _label in tool_guardrails.PROTECTED_PATH_RULES:
                try:
                    if matcher(path_obj):
                        return True, f"target inside a protected path ({target})"
                except Exception:
                    continue
        except Exception:
            pass

    if tool_name in ("shell", "ssh_exec") and target:
        try:
            from . import tool_guardrails
            import re as _re
            for pattern, label in tool_guardrails._SHELL_REGRET_PATTERNS:
                if _re.search(pattern, target):
                    return True, f"command matches regret pattern ({label})"
        except Exception:
            pass

    return False, ""


def _make_mode_approver(console, mode_state: permissions.ModeState):
    """v1.0 approver: consults the active permission mode + tool risk class.

    Tool risk arrives via the `risk=` kwarg the Registry injects (see
    tools/base.py). Capability tokens still short-circuit to True
    upstream via make_capability_aware() so a skill with explicit
    grants doesn't have to ask.

    Decision matrix per mode lives in permissions.decide().

    v1.24.0 — modal upgrade:
      * prompt uses prompt_toolkit so streaming output above keeps
        rendering while the user thinks
      * Once / Session / Always / Deny — Session adds a grant for
        (tool_name, risk) that bypasses future prompts in this session
      * mode_state.session_grants is consulted BEFORE prompting, so
        the user sees the prompt at most once per "always" tool

    v1.24.6 — high-risk prompt narrowing:
      * For protected-path writes and regret-pattern shell commands,
        suppress [s]ession and [a]lways. Only [Y]es/[N]o offered.
        Prevents fat-fingering a permanent grant that would also
        cover the unrelated cases the same (tool, risk) pair could
        approve later in the session.
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

        # ASK path. v1.24.0: check session grants BEFORE rendering anything.
        tool_name = str(kw.get("tool_name") or "")
        capability = kw.get("capability")
        grant_key = (tool_name, str(risk))
        high_risk, hr_reason = _is_high_risk_grant(tool_name, capability)
        # Even if a session grant exists, a high-risk-now request must
        # re-prompt — we don't want a `[a]lways approve fs_write` from
        # earlier to silently green-light a docs/ write.
        if (
            tool_name and not high_risk
            and mode_state.has_grant(grant_key)
        ):
            console.print(
                f"[dim]⚡ {action_label}[/] "
                f"[dim](session-approved: {tool_name})[/]"
            )
            return True

        title_suffix = (
            f"  [dim](risk={risk}, mode={mode_state.current})[/]"
        )
        if high_risk:
            title_suffix += f" [yellow](high-risk: {hr_reason})[/]"

        # v1.27.2 — structured plan-review rendering. When the action
        # is ExitPlanMode, parse the plan body for steps + file
        # references + tool-count estimates and render a dedicated
        # Plan Review panel (cyan border, metrics header, Markdown
        # body). Falls back to the regular yellow approval panel if
        # parsing or Rich rendering fails. Returns the same approver
        # bool either way — the prompt + grant logic below is shared.
        if action_label and "exit_plan_mode" in action_label.lower():
            try:
                from . import plan_render
                parsed = plan_render.parse_plan(details)
                panel = plan_render.render_rich_panel(
                    parsed, details, mode=mode_state.current,
                )
                if panel is not None:
                    console.print(panel)
                    # Plan-mode prompt is intentionally narrow — no
                    # session/always grants for "approve this plan".
                    prompt_text = "[Y]es proceed  [N]o refine  > "
                    try:
                        if HAVE_RICH:
                            from prompt_toolkit import prompt as _pt_prompt
                            ans = _pt_prompt(prompt_text, default="").strip().lower()
                        else:
                            ans = input(prompt_text).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        return False
                    if ans in ("y", "yes"):
                        return True
                    return False
            except Exception:
                # Fall through to the generic approval panel.
                pass

        # v1.25.4: when fs_write/fs_edit pass diff_data, render the
        # diff body with Rich Syntax (line numbers + diff highlighting)
        # instead of the ANSI-colored text dump. Falls back to the
        # plain Panel if Syntax rendering returns None.
        diff_data = kw.get("diff_data")
        body = details
        if diff_data and isinstance(diff_data, dict):
            try:
                from . import diff as _diff
                syntax = _diff.render_rich(
                    diff_data.get("old", ""),
                    diff_data.get("new", ""),
                    path=str(diff_data.get("path", "")),
                )
                if syntax is not None:
                    # Keep the header line (action + size summary) above
                    # the colored diff. Header is the first paragraph
                    # of `details` (everything before the blank line).
                    header = details.split("\n\n", 1)[0]
                    from rich.console import Group as _Group
                    from rich.text import Text as _Text
                    body = _Group(_Text.from_markup(header), syntax)
            except Exception:
                # Fallback to ANSI details on any rendering hiccup.
                body = details
        console.print(Panel(
            body,
            title=f"[yellow]⚠ approval needed[/]: {action_label}{title_suffix}",
            border_style="red" if high_risk else "yellow",
        ))
        # v1.24.6 #5: narrow prompt for high-risk grants.
        if high_risk:
            prompt_text = "[Y]es  [N]o  > "
        else:
            prompt_text = "[Y]es  [s]ession  [a]lways  [N]o  > "
        ans = ""
        try:
            if HAVE_RICH:
                from prompt_toolkit import prompt as _pt_prompt
                ans = _pt_prompt(prompt_text, default="").strip().lower()
            else:
                ans = input(prompt_text).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("s", "session"):
            if high_risk:
                console.print(
                    f"[yellow]→ session grant declined: {hr_reason}. "
                    f"Approve once with `y` or refuse with `n`.[/]"
                )
                return False
            if tool_name:
                mode_state.grant(grant_key)
                console.print(
                    f"[dim]→ session grant added for {tool_name}[/]"
                )
            return True
        if ans in ("a", "always"):
            if high_risk:
                console.print(
                    f"[yellow]→ persistent grant declined: {hr_reason}. "
                    f"Approve once with `y` or refuse with `n`.[/]"
                )
                return False
            # v1.24.1: persistent grant — survives janus restart.
            if tool_name:
                mode_state.grant_persistent(grant_key)
                console.print(
                    f"[dim]→ persistent grant added for {tool_name} "
                    f"(saved to ~/.janus/approvals.json; revoke with /grants)[/]"
                )
            return True
        return False

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

    # v1.24.1: shared registry (slash_dispatch.py) gets first crack at
    # the command. Falls through to the legacy if/elif chain below if
    # no shared handler is registered. Migration starts here — new
    # cross-surface commands land in slash_dispatch.py and register
    # via register_shared_handlers().
    if "_shared_slash_registry" not in state:
        from . import slash_dispatch as _sd
        reg = _sd.SlashRegistry()
        _sd.register_shared_handlers(reg)
        state["_shared_slash_registry"] = reg
    sd_reg = state["_shared_slash_registry"]
    if sd_reg.has(cmd):
        from . import slash_dispatch as _sd
        ctx = _sd.SlashContext(
            surface="cli_rich",
            state=state,
            console=console,
            print_fn=lambda s: console.print(s),
        )
        handled, result = sd_reg.dispatch(line, ctx)
        if handled and isinstance(result, str) and result:
            console.print(result)
        return handled

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
        # /memory stats — v1.18 dashboard: per-type / per-scope / recall counts.
        if target.lower() == "stats":
            from . import memory_index, memory_recall
            try:
                memory_index.reconcile()
            except Exception:
                pass
            s = memory_index.summary()
            console.print(f"[bold]Memory cards: {s['total']}[/]")
            if s["per_type"]:
                console.print("  by type:")
                for t, n in sorted(s["per_type"].items()):
                    console.print(f"    {t}: {n}")
            if s["per_scope"]:
                console.print("  by scope:")
                for sc, n in sorted(s["per_scope"].items()):
                    console.print(f"    {sc}: {n}")
            console.print(f"  total recalls: {s['total_recalls']}")
            if s["most_recalled"]:
                console.print("  most-recalled cards:")
                for r in s["most_recalled"]:
                    console.print(
                        f"    [{r['type']}:{r['subject']}] "
                        f"× {r['recall_count']}"
                    )
            log_p = config.MEMORY_DIR / "recalls.jsonl"
            if log_p.exists():
                lines = log_p.read_text("utf-8").splitlines()
                console.print(f"  recall log: {len(lines)} entries")
            paused = (config.MEMORY_DIR / "_paused").exists()
            console.print(
                f"  extraction: {'PAUSED' if paused else 'enabled'}"
            )
            # v1.19.0 Phase 6 — interview profile completion meter.
            try:
                from . import interviews, interview_runner
                state = interviews.load_state("cli", "default")
                library = interviews.load_all()
                if library:
                    pcts = interviews.compute_completion(state, library)
                    overall = interviews.overall_completion(state, library)
                    console.print(
                        f"\n[bold]Profile completion[/] "
                        f"({int(overall*100)}% overall)"
                    )
                    for line in interview_runner.render_completion_meter(pcts):
                        console.print(line)
            except Exception:
                pass
            return True
        # /memory about-me — Janus reads back current understanding.
        if target.lower() in ("about-me", "aboutme", "about me"):
            return _cmd_memory_about_me(console)
        # /memory show <id> — full card view
        if target.lower().startswith("show "):
            card_id = target[5:].strip()
            if not card_id:
                console.print("[red]usage:[/] /memory show <card-id>")
                return True
            from . import memory_cards
            p = memory_cards.card_path(card_id)
            if not p.exists():
                console.print(f"[red]card not found:[/] {card_id}")
                return True
            console.print(Markdown(p.read_text("utf-8")))
            return True
        # /memory pause — write marker file; propose_diff honors it.
        if target.lower() == "pause":
            (config.MEMORY_DIR).mkdir(parents=True, exist_ok=True)
            (config.MEMORY_DIR / "_paused").touch()
            console.print("[yellow]memory extraction paused[/]")
            return True
        # /memory resume — remove marker file.
        if target.lower() == "resume":
            marker = config.MEMORY_DIR / "_paused"
            if marker.exists():
                marker.unlink()
            console.print("[green]memory extraction enabled[/]")
            return True
        # /memory reindex — drop and rebuild the SQLite cache from cards/.
        if target.lower() == "reindex":
            from . import memory_index, memory_recall
            memory_index.reset()
            counts = memory_index.reconcile()
            memory_recall.reset_reconcile_flag()
            console.print(
                f"[green]reindexed:[/] {counts['added']} added, "
                f"{counts['updated']} updated, {counts['deleted']} dropped."
            )
            return True
        # /memory clear --type=<t> — DESTRUCTIVE wipe of one type.
        if target.lower().startswith("clear"):
            from . import memory_cards, memory_index
            rest = target[5:].strip()
            type_filter = None
            for token in rest.split():
                if token.startswith("--type="):
                    type_filter = token[len("--type="):]
            if not type_filter or type_filter not in memory_cards.TYPES:
                console.print(
                    "[red]usage:[/] /memory clear --type=<one of "
                    f"{', '.join(memory_cards.TYPES)}>"
                )
                return True
            try:
                ans = input(
                    f"clear ALL {type_filter} cards? this is destructive [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return True
            if ans not in ("y", "yes"):
                console.print("[dim]aborted[/]")
                return True
            try:
                memory_index.reconcile()
            except Exception:
                pass
            rows = memory_index.list_all(type=type_filter)
            n = 0
            for r in rows:
                memory_cards.supersede(r["id"])
                n += 1
            try:
                memory_index.reconcile()
            except Exception:
                pass
            console.print(
                f"[green]moved {n} {type_filter} card(s) to _superseded/[/]"
            )
            return True
        # /memory consolidate — manual reflection pass (Phase 8 module).
        if target.lower() == "consolidate":
            try:
                from . import memory_consolidate
            except ImportError:
                console.print("[dim]consolidate module not available[/]")
                return True
            console.print("[dim]running consolidation (LLM call)...[/]")
            try:
                summary = memory_consolidate.run_once()
            except Exception as e:
                console.print(f"[red]error:[/] {type(e).__name__}: {e}")
                return True
            console.print(
                f"[green]consolidated:[/] {summary['written']} reflection card(s) "
                f"from {summary['examined']} examined"
            )
            return True
        # /memory prune — pure-compute pruning pass.
        if target.lower() == "prune":
            from . import memory_prune
            counts = memory_prune.run_once()
            console.print(
                f"[green]pruned:[/] {counts['removed']} dropped "
                f"(active={counts['active_drops']}, "
                f"low_conf={counts['low_conf_drops']}, "
                f"superseded={counts['superseded_drops']})"
            )
            return True
        # /memory search <query> — v1.18: searches both FTS-indexed cards
        # AND the legacy substring search across .md files.
        if target.lower().startswith("search "):
            query = target[7:].strip()
            if not query:
                console.print("[red]usage:[/] /memory search <query>")
                return True
            # First: FTS5 search over cards.
            from . import memory_recall
            cards = memory_recall.top_k(
                query, top_k=10, budget_bytes=2000,
            )
            if cards:
                console.print(f"[bold]Cards ({len(cards)}):[/]")
                for c in cards:
                    console.print(
                        f"  {c['_line']}  "
                        f"[dim](id={c['id']} scope={c['scope']})[/]"
                    )
                console.print()
            # Then: legacy substring search across .md files.
            from . import memory_state
            hits = memory_state.search_memory(query, top_k=15)
            if not hits and not cards:
                console.print(f"[dim]no matches for {query!r}.[/]")
                return True
            if hits:
                console.print(f"[bold]Legacy .md matches ({len(hits)}):[/]")
                for h in hits:
                    console.print(
                        f"[bold]{h['category']}[/].md "
                        f"[dim]## {h['section']} (line {h['line_no']})[/]"
                    )
                    if h["context_above"]:
                        console.print(f"  [dim]{h['context_above']}[/]")
                    console.print(f"  {h['line']}")
                    if h["context_below"]:
                        console.print(f"  [dim]{h['context_below']}[/]")
                    console.print()
            return True
        # /memory audit  → list recent autonomous diffs from cron fires
        if target.lower() == "audit":
            audit_dir = config.MEMORY_DIR / "_audit"
            if not audit_dir.is_dir():
                console.print("[dim](no autonomous memory diffs yet)[/]")
                return True
            files = sorted(audit_dir.glob("*.md"), reverse=True)[:20]
            if not files:
                console.print("[dim](no autonomous memory diffs yet)[/]")
                return True
            for p in files:
                console.print(f"[bold]{p.name}[/]")
                console.print(Markdown(p.read_text(encoding="utf-8")))
                console.print()
            return True
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
    if cmd == "/interview":
        return _cmd_interview_rich(console, arg)
    if cmd == "/skills":
        # v1.12.0: /skills validate runs the schema check across all
        # skill files. Other /skills <args> shapes go to the existing
        # skill catalog handler.
        argl = arg.strip().lower()
        if argl == "validate":
            from . import skill_preprocessing as _sp
            issues = _sp.validate_all()
            console.print(Markdown(_sp.render(issues)))
            return True
        # v1.28.0: self-improving skills.
        # /skills suggestions       — list detected recurring patterns
        # /skills propose <id>      — LLM-draft a skill for a pattern
        # /skills decline <id>      — silence a pattern for the cooldown
        if argl == "suggestions":
            return _cmd_skills_suggestions(console, state)
        parts = arg.split(None, 1)
        if parts and parts[0].lower() == "propose":
            return _cmd_skills_propose(
                console, state, parts[1] if len(parts) > 1 else "",
            )
        if parts and parts[0].lower() == "decline":
            return _cmd_skills_decline(
                console, parts[1] if len(parts) > 1 else "",
            )
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
        # v1.28.2: extended /cost output —
        #   /cost            session summary + budget gauge if configured
        #   /cost --daily    daily spend rollup from cost.jsonl
        #   /cost --daily 14 last N days (default 7)
        argl = arg.strip()
        if argl.startswith("--daily"):
            rest = argl[len("--daily"):].strip()
            try:
                days = int(rest) if rest else 7
            except ValueError:
                days = 7
            for line in cost.render_daily(since_days=days).splitlines():
                console.print(line)
            return True
        for line in cost.render_summary().splitlines():
            console.print(line)
        gauge = cost.render_budget_line()
        if gauge:
            console.print(gauge)
        else:
            console.print(
                "  [dim](set JANUS_BUDGET_USD=<n> to enable a "
                "budget gauge)[/]"
            )
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
        cost.reset_budget_alerts()  # v1.28.2: re-arm 50/80/100% alerts
        console.print("  [green]cleared conversation turns + cost counters[/]")
        return True
    if cmd in ("/compact", "/compress"):
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
    if cmd == "/undo":
        # Drop the last user+assistant pair from BOTH the executor messages
        # list AND the persisted conversation. Useful when the model gave a
        # bad answer and you want to roll back rather than pile on context.
        msgs: list[dict] = state.get("messages") or []
        conv = state.get("conv")
        # Find indexes of last user message and the assistant block(s) that
        # followed it (which may include tool / tool_result messages in between).
        last_user = next(
            (i for i in range(len(msgs) - 1, -1, -1)
             if msgs[i].get("role") == "user"),
            None,
        )
        if last_user is None:
            console.print("[dim]nothing to undo (no prior turn)[/]")
            return True
        before = len(msgs)
        del msgs[last_user:]
        if conv is not None and conv.turns:
            conv.turns.pop()
            try:
                conversation.save(conv)
            except Exception:
                pass
        console.print(
            f"  [green]undid last turn[/] — dropped {before - len(msgs)} "
            f"messages"
        )
        return True
    if cmd == "/retry":
        # Re-run the LAST user turn through the chat loop. Drops the last
        # assistant reply (and any tool messages between it and the user
        # message) so the model gets the same input and can produce a
        # different answer. Useful when you don't want to pile on a
        # "no, try again" turn — just retry cleanly.
        msgs: list[dict] = state.get("messages") or []
        conv = state.get("conv")
        last_user_idx = next(
            (i for i in range(len(msgs) - 1, -1, -1)
             if msgs[i].get("role") == "user"),
            None,
        )
        if last_user_idx is None:
            console.print("[dim]nothing to retry (no prior turn)[/]")
            return True
        last_user = msgs[last_user_idx]
        # Drop assistant + tool messages AFTER the last user message.
        del msgs[last_user_idx + 1:]
        # Also drop the user message itself — the executor will re-append it.
        del msgs[last_user_idx]
        # Drop the trailing turn from conv.turns so the retry creates a
        # fresh entry rather than two for the same input.
        if conv is not None and conv.turns:
            conv.turns.pop()
        # Stash the request so the main loop knows to immediately retry it.
        state["__retry_input__"] = last_user.get("content") or ""
        console.print(
            f"  [green]retrying[/] last turn: "
            f"{state['__retry_input__'][:80]}"
        )
        return True
    if cmd == "/insights":
        from . import insights as _ins
        try:
            days = int(arg.strip()) if arg.strip() else 7
        except ValueError:
            console.print("[red]usage:[/] /insights [days]")
            return True
        days = max(1, min(days, 365))
        try:
            stats = _ins.compute_insights(days=days)
            console.print(Markdown(_ins.render_insights(stats)))
        except Exception as e:
            console.print(f"[red]insights failed:[/] {type(e).__name__}: {e}")
        return True
    if cmd == "/stats":
        from . import rate_limit as _rl
        try:
            console.print(Markdown(_rl.render_summary(_rl.get_summary())))
        except Exception as e:
            console.print(f"[red]stats failed:[/] {type(e).__name__}: {e}")
        return True
    if cmd in ("/pin", "/unpin"):
        conv = state.get("conv")
        if conv is None or not conv.turns:
            console.print("[dim]no conversation yet[/]")
            return True
        target = arg.strip().lower()
        if target == "last" or target == "":
            idx = len(conv.turns) - 1
        else:
            try:
                idx = int(target)
                # 1-based input → 0-based internally (matches `/undo` UX).
                if idx > 0:
                    idx -= 1
            except ValueError:
                console.print(f"[red]usage:[/] {cmd} <N|last>")
                return True
        if not 0 <= idx < len(conv.turns):
            console.print(
                f"[red]turn {idx + 1} out of range[/] "
                f"(have {len(conv.turns)} turns)"
            )
            return True
        if cmd == "/pin":
            if idx not in conv.pinned_turns:
                conv.pinned_turns.append(idx)
                conv.pinned_turns.sort()
            try:
                conversation.save(conv)
            except Exception:
                pass
            preview = (conv.turns[idx].get("request") or "")[:60]
            console.print(
                f"  [green]pinned[/] turn {idx + 1}: {preview}"
            )
        else:  # /unpin
            if idx in conv.pinned_turns:
                conv.pinned_turns.remove(idx)
                try:
                    conversation.save(conv)
                except Exception:
                    pass
                console.print(f"  [green]unpinned[/] turn {idx + 1}")
            else:
                console.print(f"[dim]turn {idx + 1} wasn't pinned[/]")
        return True
    if cmd == "/pins":
        conv = state.get("conv")
        if conv is None or not conv.pinned_turns:
            console.print("[dim](no pinned turns)[/]")
            return True
        for i in sorted(conv.pinned_turns):
            if 0 <= i < len(conv.turns):
                preview = (conv.turns[i].get("request") or "")[:80]
                console.print(f"  [yellow]{i + 1}.[/] {preview}")
        return True
    if cmd == "/resume":
        # v1.27.3 — upgraded picker. Subcommands:
        #   /resume                 → numbered list with previews (top 10)
        #   /resume <N>             → resume by 1-based index
        #   /resume <id>            → resume by exact / prefix id
        #   /resume search <query>  → filter by substring
        #   /resume gateway <name>  → filter by origin (cli_rich, telegram, ...)
        #   /resume since <date>    → filter by last_updated ≥ ISO date
        raw = arg.strip()
        sub_query = ""
        sub_gateway: str | None = None
        sub_since: str | None = None
        target = raw

        # Parse subcommands. Format: ``<sub> <value>`` where sub is
        # ``search`` / ``gateway`` / ``since``. We accept a single sub
        # at a time; chaining could come in a follow-up.
        parts = raw.split(None, 1) if raw else []
        if parts and parts[0].lower() in ("search", "gateway", "since"):
            sub = parts[0].lower()
            value = parts[1] if len(parts) > 1 else ""
            if sub == "search":
                sub_query = value
            elif sub == "gateway":
                sub_gateway = value
            elif sub == "since":
                sub_since = value
            target = ""  # filter mode renders the picker, doesn't resolve

        if target:
            conv = conversation.resolve_target(target)
            if conv is None:
                console.print(f"[red]no conversation matching[/] [bold]{target}[/]")
                return True
            state["conv"] = conv
            console.print(
                f"  [green]resumed[/] {conv.id} "
                f"({len(conv.turns)} turns, started {conv.started[:19]})"
            )
            return True

        # Picker mode. Apply filters if any sub was given.
        if sub_query or sub_gateway or sub_since:
            items = conversation.search(
                query=sub_query, gateway=sub_gateway, since=sub_since,
            )
        else:
            items = conversation.list_all()

        if not items:
            console.print("[dim]no saved conversations[/]")
            if sub_query:
                console.print(f"[dim](search: {sub_query!r} returned 0 hits)[/]")
            return True

        # Render the picker. Show #, title-or-first-msg, turns,
        # last_updated, gateway, last assistant snippet. Cap at 10 to
        # keep the panel readable; user can search to narrow further.
        t = Table(show_header=True, header_style="bold cyan")
        t.add_column("#", style="dim", width=3)
        t.add_column("title / preview", overflow="fold")
        t.add_column("turns", justify="right", width=5)
        t.add_column("last update", width=19)
        t.add_column("gateway", width=10)
        for i, item in enumerate(items[:10], start=1):
            preview = item.get("title") or item.get("first_user_msg") or "—"
            preview = preview[:60].rstrip()
            last_a = (item.get("last_assistant_msg") or "")[:60].strip()
            cell = preview
            if last_a and last_a != preview:
                cell = f"{preview}\n[dim]→ {last_a}[/]"
            t.add_row(
                str(i),
                cell,
                str(item.get("turns", 0)),
                str(item.get("last_updated", ""))[:19],
                str(item.get("gateway", "")) or "-",
            )
        console.print(t)
        if len(items) > 10:
            console.print(
                f"[dim](+{len(items) - 10} more — narrow with "
                f"`/resume search <query>` or `/resume gateway <name>`)[/]"
            )
        console.print(
            "[dim]usage: /resume <N> | <id> | search <query> | "
            "gateway <name> | since <date>[/]"
        )
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
    """`/mode [name]` — show or switch the active permission mode.

    v1.25.5: ``/mode cycle`` (or Alt+M) advances through the modes in
    a fixed order: default → acceptEdits → plan → auto →
    bypassPermissions → default. Useful for the keyboard hotkey path
    where the user doesn't want to type a specific mode name.
    """
    mode_state: permissions.ModeState = state["mode_state"]
    target = arg.strip()
    if target == "cycle":
        order = [
            permissions.DEFAULT,
            permissions.ACCEPT_EDITS,
            permissions.PLAN,
            permissions.AUTO,
            permissions.BYPASS,
        ]
        try:
            idx = order.index(mode_state.current)
        except ValueError:
            idx = -1
        target = order[(idx + 1) % len(order)]
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


def _output_ends_with_question(output: str) -> bool:
    """v1.24.5: detect when the assistant's final reply is a question
    directed at the user.

    Sam (2026-05-07) hit a UX bug: he asked Janus to run pytest, Janus
    finished and asked "Want me to install pytest-asyncio and re-run,
    or investigate the web auth failure?". The propose_diff prompt
    fired immediately and asked "apply? [y/N]: ". Sam typed "y"
    intending to answer Janus's question — but the "y" was consumed
    by the memory diff prompt, the diff was applied, and Janus's
    question was lost (next prompt was empty).

    This heuristic catches that case so we can SKIP the y/N memory
    prompt and auto-apply silently — preserving the user's
    conversational flow with the agent.
    """
    if not output:
        return False
    text = output.rstrip()
    if not text:
        return False
    # 1. Last non-empty line ends with '?'
    if text.endswith("?"):
        return True
    # 2. Look at the last ~250 chars for question-of-action phrases.
    #    These triggered the bug Sam reported even when no '?' was
    #    on the literal last line (e.g., emoji or markdown trailing).
    tail = text[-250:].lower()
    QUESTION_PHRASES = (
        "want me to",
        "should i ", "should i?",
        "would you like me to",
        "would you like to",
        "shall i ", "shall i?",
        "do you want me to",
        "let me know if",
        "let me know whether",
        "or investigate",  # exact phrase from Sam's screenshot
        "or should i",
        "which would you prefer",
    )
    return any(p in tail for p in QUESTION_PHRASES)


def _maybe_propose_memory(console, req: str, output: str,
                          cache_snap=None, trace=None) -> None:
    if not config.MEMORY_PROPOSE_ENABLED:
        return
    try:
        result = memory.propose_diff(req, output)
    except Exception as e:
        console.print(f"[dim]memory propose skipped: {type(e).__name__}: {e}[/]")
        return
    ops = result.get("ops") or []
    cards = result.get("cards") or []

    # v1.24.6 #4: harvest user-refusal events from the trace as
    # constraint cards. Pure compute — no LLM call, applies silently
    # because the user's click was already the consent gesture.
    refusal_cards: list = []
    if trace:
        try:
            from . import memory_refusal, session_context
            scope = session_context.current_scope() or "cli"
            refusal_cards = memory_refusal.cards_from_trace(
                trace, current_scope=scope,
            )
            if refusal_cards:
                written = memory.apply_cards(refusal_cards, gateway="cli")
                if written:
                    console.print(
                        f"[dim]  · recorded {len(written)} user-refusal "
                        f"constraint(s) — model will see these as cards "
                        f"on future turns (review with /memory show <id>)[/]"
                    )
        except Exception:
            # Refusal extraction is a courtesy — never block the
            # main propose_diff path on its failure.
            pass

    if not ops and not cards:
        return

    # v1.24.5: when the assistant ends with a question, the user's
    # next input is meant for the agent (their answer). Showing a
    # y/N memory prompt would steal that input. Auto-apply silently
    # instead. The user can review via `/memory` or `/grants`. This
    # behavior is opt-out via JANUS_MEMORY_PROMPT_ALWAYS=1.
    auto_apply_silent = (
        os.environ.get("JANUS_MEMORY_PROMPT_ALWAYS", "").strip().lower()
        not in ("1", "true", "yes", "on")
        and _output_ends_with_question(output)
    )
    if auto_apply_silent:
        if ops:
            memory.apply(ops)
            cats = sorted({(op.get("category") or "user") for op in ops})
            label = ", ".join(f"{c}.md" for c in cats)
            console.print(
                f"[dim]  · memory auto-applied to {label} "
                f"(assistant asked you a question — review with "
                f"/memory or revert by editing the .md files)[/]"
            )
        if cards:
            written = memory.apply_cards(cards, gateway="cli")
            if written:
                console.print(
                    f"[dim]  · auto-wrote {len(written)} card(s) to "
                    f"~/.janus/memory/cards/ "
                    f"(review with /memory show <id>)[/]"
                )
        if cache_snap is not None:
            try:
                cache_snap.refresh()
            except Exception:
                pass
        return
    if ops:
        console.print(Panel(memory.render_diff(ops), title="proposed memory updates",
                            border_style="cyan"))
    if cards:
        # v1.18: also show typed cards being proposed.
        from .memory_extract import CardProposal as _CP  # noqa: F401
        lines = []
        for c in cards:
            tag = (
                f" → {c.conflict_resolution}({c.conflict_with})"
                if c.conflict_with else ""
            )
            lines.append(
                f"  [{c.type}:{c.subject}] conf={c.confidence:.1f} "
                f"imp={c.importance:.1f} dur={c.durability:.1f} "
                f"scope={c.scope}{tag}"
            )
            lines.append(f"    {c.content[:120]}")
        console.print(Panel(
            "\n".join(lines),
            title=f"proposed memory cards ({len(cards)})",
            border_style="magenta",
        ))
    # v1.24.2: prompt_toolkit-based prompt so arrow keys + line editing
    # behave correctly under tmux/raw terminals. Pre-v1.24.2 this used
    # raw input() which echoed arrow-key escape sequences (^[[A, ^[[B)
    # into the buffer — when the user pressed Enter, the answer didn't
    # match "y"/"yes" and was silently denied, leaving the CLI in a
    # confusing "stuck" state.
    try:
        if HAVE_RICH:
            from prompt_toolkit import prompt as _pt_prompt
            ans = _pt_prompt("apply? [y/N]: ", default="").strip().lower()
        else:
            ans = input("apply? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans in ("y", "yes"):
        if ops:
            memory.apply(ops)
            # v1.17.0 — print the actual categories the ops touched, not a
            # hardcoded "user.md". Pre-v1.17 this lied: if the model proposed
            # a project.md / soul.md / preferences.md update, the UI claimed
            # it landed in user.md even though memory.apply correctly routed
            # it. The DATA was right; the DISPLAY was wrong.
            cats = sorted({(op.get("category") or "user") for op in ops})
            label = ", ".join(f"{c}.md" for c in cats)
            console.print(f"  [green]applied to {label}[/]")
        if cards:
            written = memory.apply_cards(cards, gateway="cli")
            if written:
                console.print(
                    f"  [green]wrote {len(written)} card(s) to "
                    f"~/.janus/memory/cards/[/]"
                )
        if cache_snap is not None:
            cache_snap.preamble = cache.snapshot().preamble


# ---------- Clarify callback (v1.8.0) ----------


def _make_console_clarify_cb(console):
    """Console-side callback for the clarify tool.

    Renders the model's question, lists choices (numbered) when present,
    and reads one line from stdin. Empty answer → return None so the
    tool emits the UNAVAILABLE sentinel (model picks a default).
    """
    def _cb(question: str, choices: list[str] | None) -> str | None:
        console.print()
        console.print(f"[bold yellow]? {question}[/]")
        if choices:
            for i, c in enumerate(choices, 1):
                console.print(f"  [cyan]{i}.[/] {c}")
            console.print(f"  [dim]{len(choices) + 1}. (other / type your own)[/]")
        try:
            ans = input("clarify > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not ans:
            return None
        # Numeric pick → resolve to choice text.
        if choices and ans.isdigit():
            idx = int(ans)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
            # else: fall through, treat as free text
        return ans
    return _cb


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

    # v1.25.5: hotkeys. Each binding injects the matching slash command
    # into the prompt buffer and accepts it, so behavior is identical
    # to the user typing the slash by hand. Predictable + reuses every
    # existing slash code path (retry, undo, mode, clear, plan, help).
    bindings = KeyBindings()

    def _inject_and_submit(event, text: str) -> None:
        """Replace the current buffer with ``text`` and submit it.

        Identical to the user typing the slash and pressing Enter —
        all subsequent dispatch reuses the existing slash handlers.
        """
        try:
            buf = event.current_buffer
            buf.text = text
            buf.cursor_position = len(text)
            buf.validate_and_handle()
        except Exception:
            # If the buffer isn't ready (e.g. completer popup open),
            # fall through silently — user can press the hotkey again.
            pass

    @bindings.add("c-r")
    def _retry(event):
        """Ctrl+R — retry last user input (re-runs /retry slash)."""
        _inject_and_submit(event, "/retry")

    @bindings.add("c-z")
    def _undo(event):
        """Ctrl+Z — drop the last user+assistant pair (/undo)."""
        _inject_and_submit(event, "/undo")

    @bindings.add("c-l")
    def _clear(event):
        """Ctrl+L — clear screen (/clear)."""
        _inject_and_submit(event, "/clear")

    # Ctrl+M historically maps to Enter on many terminals, so we use
    # Esc-m (Alt+M on most keyboards) for mode cycling instead. That's
    # the prompt_toolkit-canonical way to bind Alt+<key>.
    @bindings.add("escape", "m")
    def _mode_cycle(event):
        """Alt+M — cycle through permission modes."""
        _inject_and_submit(event, "/mode cycle")

    @bindings.add("escape", "p")
    def _plan_toggle(event):
        """Alt+P — toggle plan mode on/off."""
        # Read current mode out of state so we toggle correctly.
        cur = mode_state.current
        target = "default" if cur == "plan" else "plan"
        _inject_and_submit(event, f"/mode {target}")

    @bindings.add("escape", "h")
    def _hotkeys_help(event):
        """Alt+H — print the hotkey cheatsheet inline."""
        # Run before the buffer accepts so the help text appears
        # ABOVE the prompt and the buffer stays empty for the next
        # input the user types.
        def _show():
            console.print(
                "[bold]janus hotkeys[/]\n"
                "  [cyan]Ctrl+R[/]  retry last input\n"
                "  [cyan]Ctrl+Z[/]  undo last user+assistant pair\n"
                "  [cyan]Ctrl+L[/]  clear screen\n"
                "  [cyan]Alt+M[/]   cycle mode "
                "(default → acceptEdits → plan → auto → bypassPermissions)\n"
                "  [cyan]Alt+P[/]   toggle plan mode\n"
                "  [cyan]Alt+H[/]   show this cheatsheet\n"
                "  [cyan]Ctrl+C[/]  cancel current turn\n"
                "  [cyan]Ctrl+D[/]  exit (when prompt is empty)\n"
                "  [cyan]Esc+Enter[/]  multi-line input"
            )
        try:
            event.app.run_in_terminal(_show)
        except Exception:
            _show()

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
        # v1.25.5: hotkeys (defined above as `bindings`). Ctrl+R retry,
        # Ctrl+Z undo, Ctrl+L clear, Alt+M mode cycle, Alt+P plan
        # toggle, Alt+H help.
        key_bindings=bindings,
    )

    cache_snap = cache.snapshot()
    pending = conversation.take_pending()
    state["conv"] = pending if pending is not None else conversation.new(gateway="cli_rich")
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
        # v1.9.0: /retry can stash the prior input here. Skip the prompt
        # and immediately re-process it as if the user typed it again.
        retry_input = state.pop("__retry_input__", None)
        if retry_input:
            req = str(retry_input).strip()
        else:
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

        # v1.25.1: expand `@path` mentions into inlined file contents
        # before the model sees the message. The original (un-expanded)
        # text is preserved in `last_user_input` for /retry and the
        # conversation log so the user's intent stays readable.
        state["last_user_input"] = req
        if "@" in req:
            try:
                from . import at_mentions as _am
                expanded, _at_log = _am.expand_at_mentions(
                    req, workspace=config.WORKSPACE,
                )
                if _at_log:
                    # One-line summary of what got injected so the user
                    # sees what's going to the model.
                    ok = [e for e in _at_log if e["status"] in ("ok", "truncated")]
                    skipped = [e for e in _at_log if e not in ok]
                    if ok:
                        names = ", ".join(e["path"] for e in ok)
                        console.print(
                            f"[dim]   📎 inlined {len(ok)} file(s): {names}[/]"
                        )
                    if skipped:
                        for e in skipped:
                            console.print(
                                f"[yellow dim]   ⚠ @{e['path']}: "
                                f"{e['status']} (left literal)[/]"
                            )
                    req = expanded
            except Exception as _exc:
                # @-mention failure must never block the turn.
                console.print(
                    f"[dim]   @-mention expansion skipped: "
                    f"{type(_exc).__name__}[/]"
                )

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
            # v1.17.0 — only surface skill matches when the user message
            # is substantive AND the match set differs from last turn.
            # Pre-v1.17 this printed on EVERY turn (including "yes" / "ok"
            # / one-word answers) which was pure visual noise.
            names = tuple(s.name for s in matches[:3])
            if len(req.strip()) >= 30 and state.get("_last_match_set") != names:
                state["_last_match_set"] = names
                console.print(
                    f"[dim]matching skills (not auto):[/] {', '.join(names)} "
                    f"[dim](promote one to attach automatically)[/]"
                )

        caps = attached_skill.capabilities if attached_skill else CapabilitySet()
        tools = default_registry(capabilities=caps)
        # v1.8.0: replace the bundled callback-less Clarify with one that
        # actually prompts in the rich console. Drop the old registration
        # first so the model sees a single tool definition.
        from .tools.clarify import Clarify as _Clarify
        tools.remove_tool("clarify")
        tools.add_tool(_Clarify(callback=_make_console_clarify_cb(console)))
        approver = make_protected(base_approver, caps, mode_state.current)

        # v1.19.0 Phase 4: drip-mode pre-turn — if a drip question was
        # asked last turn and is still pending, treat user input as the
        # answer (write a high-confidence card). The user's input ALSO
        # goes to the executor as a normal chat turn, so they can both
        # answer and chat in one message.
        try:
            from . import interviews as _iv
            drip_handled, drip_ack = _iv.consume_pending_drip_answer(
                "cli", "default", req,
            )
            if drip_handled and drip_ack:
                console.print(f"[green dim]→ {drip_ack}[/]")
        except Exception:
            pass

        # v1.5.1: emit a thinking indicator BEFORE the first model call
        # so the user sees Janus is alive during the silent gather phase
        # (multiple fs_read / web_fetch / etc. before the first text
        # token). Without this, long task setups look like a hang.
        # v1.24.4: replaced the static thinking heartbeat with a live
        # status line (status_line.StatusLine) — updates every 400ms
        # showing elapsed time + token count + current verb. Falls
        # back to the old static line on non-TTY / JANUS_NO_STATUS_LINE.
        from . import status_line as _sl
        status = _sl.StatusLine()
        state["_status_line"] = status
        if status._disabled:
            console.print("[magenta dim]⚡ thinking…[/magenta dim]")
        status.start()

        try:
            t0 = time.time()
            step_renderer = _render_step_factory(console, state)
            # v1.25.0 Phase 0: route through the surface-agnostic event
            # stream instead of calling executor.chat directly. run_turn
            # is a drop-in (same kwargs, same return tuple), but the
            # underlying iteration goes through app.chat_events so any
            # future event-level features (hook_fired, memory_recall,
            # keepalive…) reach this surface for free.
            output, trace = app.run_turn(
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
        finally:
            # status.stop is idempotent; runs even on continue/return.
            status.stop()
            state.pop("_status_line", None)

        # v1.19.0 Phase 4: drip-mode post-turn — after the assistant
        # replies, ask the next drip question (one per turn while quota
        # remains). Best-effort; never break the chat loop.
        try:
            from . import interviews as _iv
            drip_q = _iv.get_drip_question("cli", "default")
            if drip_q is not None:
                question_text, _fqid = drip_q
                console.print(
                    f"\n[cyan]{branding.glyph('💬', '?')} quick question:[/] {question_text}\n"
                    f"[dim](answer normally, 'skip' to skip, "
                    f"'stop drip' to pause)[/]"
                )
        except Exception:
            pass

        # v1.19.0 Phase 7: inferred-suggestion offer. propose_diff (which
        # ran inside executor.chat) may have queued ONE offer based on
        # category hints in the conversation. Show it once, pop it; the
        # model handles "yes"/"no" interpretation in the next turn.
        try:
            from . import interview_inferred as _inf
            offer = _inf.pop_pending("cli", "default")
            if offer is not None:
                console.print(
                    f"\n[yellow]{branding.glyph('💡', '*')}[/] {_inf.render_offer(offer)}"
                )
        except Exception:
            pass

        # v1.28.1: skill-proposal auto-offer. After the turn completes,
        # check if any strong recurring patterns are surfacing in the
        # session + recent log. Show the TOP one only (capped at one
        # offer per turn so we're not spammy). mark_offered triggers
        # the 7-day cooldown — re-running the same routine tomorrow
        # won't re-trigger the offer.
        try:
            from . import skill_proposer as _sp
            patterns = _sp.list_offerable(current_trace=trace)
            if patterns:
                top = patterns[0]
                if top.occurrences >= _sp.AUTO_OFFER_MIN_OCCURRENCES:
                    console.print(
                        f"\n[cyan]{branding.glyph('🪄', '+')}[/] "
                        f"{top.description}. "
                        f"[dim]/skills propose {top.id}[/] to draft, "
                        f"[dim]/skills decline {top.id}[/] to silence."
                    )
                    _sp.mark_offered(top.id)
        except Exception:
            pass

        # v1.28.2: budget alert. When session spend just crossed a
        # 50/80/100% threshold of JANUS_BUDGET_USD, print a one-line
        # warning. Each threshold fires AT MOST ONCE per session
        # (via cost._ALERTED_THRESHOLDS). Re-armed by /clear or
        # cost.reset_budget_alerts(). Best-effort wrap.
        try:
            crossed = cost.check_budget_alerts()
            if crossed:
                status = cost.budget_status()
                # Take the highest threshold crossed for the line
                top_t = max(crossed)
                colour = "red" if top_t >= 1.0 else "yellow"
                pct = status["percent"] * 100
                console.print(
                    f"\n[{colour}]{branding.glyph('⚠', '!')} "
                    f"budget alert:[/] crossed {int(top_t * 100)}% "
                    f"({pct:.1f}% of ${status['budget']:.2f} — "
                    f"${status['spent']:.4f} spent)"
                )
        except Exception:
            pass

        # v1.15.0 — detect ExitPlanMode approval. The trace records
        # tool calls as type='tool_call' with a 'result_preview' field
        # holding the tool's stringified return value. PLAN_APPROVED is
        # the sentinel the model received; we flip mode to default.
        from .tools.plan_mode import PLAN_APPROVED
        for entry in trace:
            if (
                isinstance(entry, dict)
                and entry.get("tool") == "exit_plan_mode"
                and PLAN_APPROVED in str(entry.get("result_preview") or "")
            ):
                if mode_state.current == permissions.PLAN:
                    mode_state.current = permissions.DEFAULT
                    console.print(
                        "[green]✓ plan approved[/] — mode switched to "
                        "[bold]default[/]"
                    )
                break

        # Render any final text the model produced. Stream already wrote
        # most of it; this is the markdown re-render for fenced code blocks
        # etc. only when a long markdown-shaped reply came back without
        # streaming, or when the user wants a different output style.
        rendered = output_styles.render(output, state.get("output_style", "markdown"))
        # v1.24.3: drop emoji glyphs when the terminal can't render them.
        # No-op on healthy UTF-8 terminals.
        rendered = branding.emoji_safe_text(rendered)
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
            # v1.9.0: auto-name the conversation after the first turn so
            # /resume and /insights show meaningful labels instead of
            # opaque timestamp+hex IDs.
            try:
                from . import title_generator as _tg
                _tg.maybe_generate(conv)
            except Exception:
                pass
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

        _maybe_propose_memory(console, req, output,
                              cache_snap=cache_snap, trace=trace)

    console.print(f"[dim]bye.[/]")


def _render_step_factory(console, state=None):
    """state is the cli_rich main-loop state dict; we read `verbose` and
    use `_stream_buffer` to flush stream chunks correctly."""
    state = state or {}
    state.setdefault("_stream_buffer", "")

    def _status() -> "object | None":
        """v1.24.4: short helper to fetch the active StatusLine.
        Returns None if status is disabled or not started."""
        return state.get("_status_line")

    def _clear_status_for_print() -> None:
        sl = _status()
        if sl is not None:
            sl.clear()

    def render_step(step: dict) -> None:
        sl = _status()
        if step["type"] == "model_start":
            # v1.5.3: heartbeat at every LLM call boundary so the user
            # sees activity at every step, not just step 0. Step 0 is
            # the initial "⚡ thinking…" emitted by the chat handler;
            # steps 1+ are subsequent model turns within the same
            # user-facing turn (post-tool-call cycles).
            step_num = step.get("step", 0)
            if sl is not None:
                sl.set_verb("calling model" if step_num > 0 else "thinking")
            if step_num > 0:
                _flush_stream(console, state)
                _clear_status_for_print()
                # Avoid printing a per-step heartbeat line if the live
                # status is active — the status already shows what's
                # happening. Only print on the static fallback.
                if sl is None or sl._disabled:
                    console.print(
                        f"[magenta dim]⚡ step {step_num + 1} — calling model…[/]"
                    )
        elif step["type"] == "tool_call":
            verbose = state.get("verbose", False)
            limit = 200 if verbose else 60
            args_brief = ", ".join(
                f"{k}={(str(v) if len(str(v)) < limit else str(v)[:limit-3] + '…')}"
                for k, v in step["args"].items()
            )
            # Flush any in-flight stream chunk first so it doesn't get
            # interleaved with the tool call line.
            _flush_stream(console, state)
            _clear_status_for_print()
            if sl is not None:
                # v1.24.4: switch verb to the per-tool description.
                from . import status_line as _sl
                sl.set_verb(_sl.verb_for_tool(step["tool"]))
            console.print(
                f"[dim]   {branding.TOOL_CALL_ARROW} {step['tool']}({args_brief})[/]"
            )
        elif step["type"] == "tool_result":
            _clear_status_for_print()
            tool_name = step.get("tool") or step.get("name") or ""
            preview = step.get("result_preview", "")
            # v1.25.3: when the model writes/reads the todo list, render
            # the on-disk state as a checklist panel instead of the bare
            # one-line preview. Same shape as Claude Code's task panel.
            if tool_name in ("todo_write", "todo_read"):
                try:
                    from . import task_render
                    todos = task_render.parse_todos_from_disk(
                        config.TODOS_FILE,
                    )
                    panel = task_render.render_rich_panel(todos)
                    if panel is not None:
                        console.print(panel)
                    else:
                        # Rich missing for some reason; fall through to
                        # the plain preview below.
                        head = preview.splitlines()[0][:80] if preview else ""
                        if head:
                            console.print(
                                f"[dim]    {branding.TOOL_OK} {head}[/]"
                            )
                except Exception:
                    head = preview.splitlines()[0][:80] if preview else ""
                    if head:
                        console.print(
                            f"[dim]    {branding.TOOL_OK} {head}[/]"
                        )
            else:
                head = preview.splitlines()[0][:80] if preview else ""
                if head:
                    console.print(f"[dim]    {branding.TOOL_OK} {head}[/]")
            # v1.24.4: refresh token count from cost.turn_stats — gives
            # the user a sense of how the model burn is progressing.
            if sl is not None:
                try:
                    from . import cost as _cost
                    ts = _cost.turn_stats()
                    sl.set_tokens(
                        int(ts.prompt_tokens) + int(ts.completion_tokens),
                    )
                except Exception:
                    pass
                # Verb back to "thinking" while we wait for the next
                # model call after this tool's result.
                sl.set_verb("thinking")
        elif step["type"] == "recovered_tool_call":
            # v1.17.2 — model emitted a tool call as JSON in content
            # rather than a proper tool_calls field. Janus recovered it
            # so the loop could continue, but the user should know the
            # endpoint is misconfigured (likely missing
            # --enable-auto-tool-choice on the vLLM side).
            _flush_stream(console, state)
            _clear_status_for_print()
            console.print(
                f"[yellow]⚠ recovered tool call from content:[/] "
                f"[bold]{step.get('tool', '?')}[/] "
                f"[dim](endpoint missing --enable-auto-tool-choice?)[/]"
            )
        elif step["type"] == "nudge":
            # v1.17.0 / v1.17.1 — assistant returned empty/stall content;
            # injected a system reminder and retrying. Surfacing this
            # tells the user "yes, the model stalled — Janus is pushing
            # it to continue" rather than leaving a silent gap.
            _flush_stream(console, state)
            _clear_status_for_print()
            if sl is not None:
                sl.set_verb("nudging stalled model")
            reason = step.get("reason", "?")
            preview = step.get("preview", "")
            console.print(
                f"[magenta dim]↻ nudge ({reason}) — "
                f"{('model stalled: ' + preview[:60]) if preview else 'retrying'}[/]"
            )
        elif step["type"] == "stream_chunk":
            text = step.get("text", "")
            if text:
                # v1.24.3: strip emoji on terminals where they'd
                # render as mojibake (broken locale / non-UTF-8 stdout).
                # On healthy terminals this is a no-op.
                text = branding.emoji_safe_text(text)
                # v1.24.4: pause the status spinner while we own the
                # cursor for raw stream writes.
                if sl is not None and not state.get("_stream_buffer"):
                    sl.begin_streaming()
                # Use raw stdout write for unbuffered streaming feel; rich
                # would re-render the whole panel for each chunk.
                console.file.write(text)
                console.file.flush()
                state["_stream_buffer"] = state.get("_stream_buffer", "") + text
        elif step["type"] == "final":
            _flush_stream(console, state)
            if sl is not None:
                sl.end_streaming()
                sl.clear()
        elif step["type"] == "soft_cap_warning":
            _clear_status_for_print()
            if sl is not None:
                sl.set_verb("wrapping up (soft cap)")
        elif step["type"] == "step_limit_reached":
            _clear_status_for_print()
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
