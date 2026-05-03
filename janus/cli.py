"""cli.py -- basic terminal UI (input() loop). cli_rich provides the polished one.

v1.0: Claude-Code-shaped chat loop. The interpretation picker moved to
the /why slash command. Mode-aware approver consults permissions.decide().
Mirror of cli_rich's behavior in plain ANSI for users without
prompt_toolkit/rich installed.
"""
from __future__ import annotations
import sys
import time
from typing import Any

from . import config, interpreter, executor, logger, memory, index, skills
from . import eval as eval_mod, planner, orchestrator, skill_evolution
from . import skills_market, hooks, cache, branding, conversation, cost, skill_catalog
from . import statusline, commands as commands_mod, doctor, init_codebase
from . import output_styles, permissions
from .mcp import client as mcp_client
from .tools import default_registry, make_capability_aware, CapabilitySet


class C:
    R = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    BLUE = "\033[34m"; CYAN = "\033[36m"; GREEN = "\033[32m"
    YELLOW = "\033[33m"; RED = "\033[31m"; MAGENTA = "\033[35m"


_RUN_STATE: dict = {
    "conv": None,
    "verbose": False, "turn": 0,
    "output_style": config.OUTPUT_STYLE,
    "custom_commands": {},  # populated at session start
    "mode_state": permissions.ModeState(),
    # v1.0: full conversation message list across turns.
    "messages": [],
    "last_user_input": "",
}


def banner():
    """Bifurcation logo + status block + commands hint."""
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

    print()
    for logo, title in branding.logo_with_titles(b):
        if title:
            print(f"{C.MAGENTA}{logo}{C.R}{C.BOLD}{title}{C.R}"
                  if title.strip().startswith("janus")
                  else f"{C.MAGENTA}{logo}{C.R}{C.DIM}{title}{C.R}")
        else:
            print(f"{C.MAGENTA}{logo}{C.R}")
    print()
    for line in branding.status_lines(b):
        print(f"{C.DIM}{line}{C.R}")
    print()
    print(f"{C.DIM}   {branding.COMMANDS_HINT}{C.R}\n")


def show_interpretations(interps, *, width=72):
    """Boxed interpretation cards. Risk lives in the card border so the eye
    finds it without reading the body."""
    print()
    for i, x in enumerate(interps, 1):
        label = str(x.get('label', '')).strip() or "(unlabeled)"
        action = str(x.get('action', '')).strip()
        risk = str(x.get('risk', '')).strip() or "—"
        _print_interp_card(i, label, action, risk, width)


def _print_interp_card(i, label, action, risk, width):
    head = f"[{i}] {label}"
    dashes = max(1, width - 8 - len(head) - len(risk))
    print(
        f"  {C.CYAN}┌─ {C.BOLD}{head}{C.R}{C.CYAN} "
        f"{'─' * dashes} {C.YELLOW}{risk}{C.CYAN} ─┐{C.R}"
    )
    body_w = width - 4
    for line in _wrap_text(action, body_w):
        padded = line + " " * (body_w - len(line))
        print(f"  {C.CYAN}│{C.R}  {padded}  {C.CYAN}│{C.R}")
    print(f"  {C.CYAN}└{'─' * (width - 2)}┘{C.R}")


def _wrap_text(text, w):
    if not text:
        return [""]
    words, lines, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + (1 if cur else 0) <= w:
            cur = (cur + " " + word) if cur else word
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def prompt_choice(n):
    while True:
        try:
            ch = input(f"   pick [1-{n}], (r)efine, (s)kip, (q)uit  {branding.PROMPT_GLYPH} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"
        if ch in ("q", "r", "s"):
            return ch
        if ch.isdigit() and 1 <= int(ch) <= n:
            return int(ch)
        print(f"   {C.RED}invalid.{C.R}")


def prompt_skill_attach(matches):
    if not matches:
        return None
    top = matches[0]
    if top.state == "trusted-auto":
        print(f"{C.DIM}auto-attached skill:{C.R} {top.name}")
        return top
    print(f"\n{C.DIM}matching skills:{C.R}")
    for i, s in enumerate(matches[:5], 1):
        print(f"  {C.BOLD}[{i}]{C.R} {s.name} ({s.state}) -- {s.description}")
    try:
        ch = input(f"attach skill? [1-{min(5,len(matches))}/enter to skip]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if ch.isdigit() and 1 <= int(ch) <= min(5, len(matches)):
        return matches[int(ch) - 1]
    return None


def make_mode_approver(mode_state: permissions.ModeState):
    """v1.0 approver: consults permission mode + tool risk class.

    Same matrix as cli_rich, plain ANSI rendering.
    """
    def approver(label, details, **kw):
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        decision = permissions.decide(risk, mode_state.current)

        if decision == permissions.ALLOW:
            return True
        if decision == permissions.DENY:
            print(
                f"  {C.YELLOW}✗ {label}{C.R} "
                f"{C.DIM}blocked by mode '{mode_state.current}' (risk={risk}){C.R}"
            )
            return False
        # ASK — show the panel + y/N.
        print(
            f"\n{C.YELLOW}[approval] {C.BOLD}{label}{C.R}  "
            f"{C.DIM}(risk={risk}, mode={mode_state.current}){C.R}"
        )
        for line in details.splitlines():
            print(f"  {line}")
        try:
            return input("approve? [y/N]: ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False
    return approver


# Back-compat alias for any external caller.
make_approver = make_mode_approver


def render_step(step):
    if step["type"] == "tool_call":
        verbose = _RUN_STATE.get("verbose", False)
        if verbose:
            args_brief = ", ".join(
                f"{k}={(str(v)[:200]+'...' if len(str(v))>=200 else str(v))}"
                for k, v in step["args"].items()
            )
        else:
            args_brief = ", ".join(
                f"{k}={(str(v)[:57]+'...' if len(str(v))>=60 else str(v))}"
                for k, v in step["args"].items()
            )
        print(f"{C.DIM}   {branding.TOOL_CALL_ARROW} {step['tool']}({args_brief}){C.R}")
    elif step["type"] == "tool_result":
        preview = step.get("result_preview", "")
        head = preview.splitlines()[0][:80] if preview else ""
        print(f"{C.DIM}    {branding.TOOL_OK} {head}{C.R}" if head else "")


def _read_input_with_continuation(primary_prompt: str,
                                  continuation_prompt: str) -> str:
    """Read a single logical line. Lines ending in `\\` (backslash) continue
    on the next line — same affordance as bash. Empty continuation = submit.
    """
    parts: list[str] = []
    line = input(primary_prompt)
    while line.endswith("\\") and not line.endswith("\\\\"):
        parts.append(line[:-1])
        nxt = input(continuation_prompt)
        if not nxt:
            line = ""
            break
        line = nxt
    parts.append(line)
    return "\n".join(parts)


def feedback():
    try:
        fb = input(f"{C.DIM}   feedback  +good  -bad  enter to skip  {branding.PROMPT_GLYPH} {C.R}").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return {"+": "good", "-": "bad"}.get(fb)


# ---------- Slash commands ----------


def handle_command(line):
    if not line.startswith("/"):
        return False
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    # Phase 15: user-defined slash commands. They expand to a regular request
    # and we return the rewritten request rather than True so the main loop
    # processes it through the interpreter normally.
    custom = _RUN_STATE.get("custom_commands") or {}
    custom_name = cmd[1:]  # drop leading /
    if custom_name in custom:
        rendered = custom[custom_name].render(arg)
        # Stash the rewrite so main loop picks it up.
        _RUN_STATE["_pending_custom"] = rendered
        return False  # NOT a built-in: main loop reads _pending_custom

    if cmd == "/mode":
        return _cmd_mode(arg)
    if cmd == "/why":
        return _cmd_why()
    if cmd == "/workspace":
        return _cmd_workspace(arg)
    if cmd == "/analyze":
        analyze(); return True
    if cmd == "/memory":
        return _cmd_memory(arg)
    if cmd == "/search":
        return _cmd_search(arg)
    if cmd == "/skills":
        return _cmd_skills(arg)
    if cmd == "/promote":
        return _cmd_promote(arg)
    if cmd == "/skill":
        return _cmd_skill_new(arg)
    if cmd == "/eval":
        return _cmd_eval(arg)
    if cmd == "/mcp":
        return _cmd_mcp(arg)
    if cmd == "/cost":
        return _cmd_cost()
    if cmd == "/init":
        return _cmd_init()
    if cmd == "/model":
        return _cmd_model(arg)
    if cmd == "/doctor":
        return _cmd_doctor()
    if cmd == "/output-style" or cmd == "/style":
        return _cmd_output_style(arg)
    if cmd == "/commands":
        return _cmd_list_commands()
    if cmd == "/verbose":
        v = arg.strip().lower()
        if v in ("on", "true", "1"):
            _RUN_STATE["verbose"] = True
            print(f"  {C.GREEN}verbose mode ON{C.R}")
        elif v in ("off", "false", "0"):
            _RUN_STATE["verbose"] = False
            print(f"  {C.YELLOW}verbose mode OFF{C.R}")
        else:
            print(f"  verbose mode: {'on' if _RUN_STATE.get('verbose') else 'off'}")
        return True
    if cmd == "/clear":
        return _cmd_clear()
    if cmd == "/compact":
        return _cmd_compact()
    if cmd == "/resume":
        return _cmd_resume(arg)
    if cmd == "/continue":
        return _cmd_continue()
    if cmd == "/triggers":
        return _cmd_triggers()
    if cmd == "/help":
        print("/mode /why /workspace /memory /search /skills /promote /skill {new|review|import} /cost /clear /compact /resume [id] /continue /eval [--last N] [--skill <n>] /mcp {list|connect|disconnect} /triggers /analyze q")
        return True
    print(f"  {C.RED}unknown: {cmd}{C.R}")
    return True


def _cmd_mode(arg: str) -> bool:
    """`/mode [name]` — show or switch the active permission mode."""
    mode_state: permissions.ModeState = _RUN_STATE["mode_state"]
    target = arg.strip()
    if not target:
        rows = [
            (permissions.DEFAULT, "read auto · write/exec ask"),
            (permissions.ACCEPT_EDITS, "read+write auto · exec ask"),
            (permissions.PLAN, "read auto · write/exec DENY"),
            (permissions.BYPASS, "everything auto · no prompts"),
        ]
        print()
        for name, desc in rows:
            marker = "● " if name == mode_state.current else "  "
            color = C.MAGENTA if name == mode_state.current else C.R
            print(f"  {color}{marker}{name:<18}{C.R}  {C.DIM}{desc}{C.R}")
        print(f"\n  {C.DIM}usage: /mode <name>  ·  current: {C.BOLD}{mode_state.current}{C.R}")
        return True
    normalized = permissions.normalize(target)
    if normalized == permissions.DEFAULT and target.lower() not in (
        "manual", "default"
    ) and target not in permissions.ALL_MODES:
        print(
            f"  {C.RED}unknown mode: {target}{C.R}  "
            f"{C.DIM}valid: {', '.join(permissions.ALL_MODES)}{C.R}"
        )
        return True
    mode_state.set(normalized)
    color = C.RED if normalized == permissions.BYPASS else C.GREEN
    print(f"  {color}mode -> {mode_state.current}{C.R}")
    if normalized == permissions.BYPASS:
        print(
            f"  {C.RED}warning:{C.R} every tool will run without asking. "
            f"{C.DIM}/mode default to disable.{C.R}"
        )
    logger.write({
        "ts": logger.now_iso(),
        "type": "mode_switch",
        "new_mode": normalized,
    })
    return True


def _cmd_why() -> bool:
    """`/why` — re-interpret the last user message and surface 2-3 candidates."""
    last = _RUN_STATE.get("last_user_input", "")
    if not last:
        print(f"  {C.DIM}nothing to interpret yet — type a message first{C.R}")
        return True
    print(
        f"  {C.DIM}re-interpreting:{C.R} "
        f"{last[:80]}{'...' if len(last) > 80 else ''}"
    )
    conv = _RUN_STATE.get("conv")
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
        print(f"  {C.RED}interpreter failed: {e}{C.R}")
        return True
    if not interps:
        print(f"  {C.DIM}(no interpretations returned){C.R}")
        return True
    show_interpretations(interps)
    print(
        f"  {C.DIM}This is read-only — none of these were executed. "
        f"Send a new message to act.{C.R}"
    )
    return True


def _cmd_workspace(arg):
    if not arg:
        print(f"  workspace: {config.WORKSPACE}"); return True
    from pathlib import Path
    new = Path(arg).expanduser().resolve()
    if not new.is_dir():
        print(f"  {C.RED}not a directory: {new}{C.R}")
    else:
        config.WORKSPACE = new; print(f"  workspace -> {new}")
    return True


def _cmd_memory(arg=""):
    """`/memory` — show all categories. `/memory <cat>` — show one category.

    v1.3: memory is multi-category at ~/.janus/memory/<cat>.md.
    """
    arg = (arg or "").strip()
    if arg:
        txt = memory.read(arg)
        if not txt:
            print(f"  {C.DIM}(no {arg}.md yet){C.R}")
        else:
            print(); print(txt)
        return True
    cats = memory.list_categories()
    configured = list(config.MEMORY_CATEGORIES)
    if not cats:
        print(f"  {C.DIM}(no memory yet){C.R}")
        print(f"  {C.DIM}categories ready to populate: "
              f"{', '.join(configured)}{C.R}")
        return True
    print()
    for cat in cats:
        body = memory.read(cat).strip()
        sz = len(body)
        print(f"  {C.BOLD}{cat}.md{C.R} {C.DIM}({sz} bytes){C.R}")
        for ln in body.splitlines():
            print(f"    {ln}")
        print()
    # Also list configured-but-empty so user knows what's available.
    empty = [c for c in configured if c not in cats]
    if empty:
        print(f"  {C.DIM}empty: {', '.join(c + '.md' for c in empty)}{C.R}")
    return True


def _cmd_search(arg):
    if not arg.strip():
        print(f"  {C.RED}usage: /search <query>{C.R}"); return True
    index.sync()
    hits = index.search(arg, k=10)
    if not hits:
        print(f"  {C.DIM}no matches.{C.R}"); return True
    print()
    for h in hits:
        print(f"  {C.BOLD}{h.ts[:19]}{C.R} {C.CYAN}{h.request[:80]}{C.R}")
        if h.tools_used:
            print(f"    {C.DIM}tools: {h.tools_used}{C.R}")
    return True


def _cmd_skills(arg=""):
    arg = (arg or "").strip()
    if arg.startswith("install-bundled"):
        rest = arg[len("install-bundled"):].strip()
        return _cmd_skills_install_bundled(force=(rest == "--force"))
    items = skills.list_skills()
    if arg:
        items = skill_catalog.filter_skills(items, arg)
        if not items:
            print(f"  {C.DIM}no skills match '{arg}'{C.R}"); return True
    elif not items:
        print(f"  {C.DIM}no skills yet -- /skills install-bundled or /skill new{C.R}")
        return True
    print()
    for s in items:
        score = s.trust_score()
        score_label = (
            f"{int(score*100)}%" if score is not None else "—"
        )
        runs_part = (
            f"{C.DIM}runs={s.runs} ({s.success}/{s.fail}) trust={score_label} {s.trust_label()}{C.R}"
            if s.runs else
            f"{C.DIM}no runs yet{C.R}"
        )
        print(f"  {C.BOLD}{s.name}{C.R} ({s.state}) -- {s.description}")
        print(f"    {runs_part}")
    return True


def _cmd_skills_install_bundled(*, force=False):
    result = skill_catalog.install_bundled(force=force)
    inst, skip, errs = result["installed"], result["skipped"], result["errors"]
    if not inst and not skip and not errs:
        print(f"  {C.DIM}no bundled skills to install{C.R}")
        return True
    if inst:
        print(f"  {C.GREEN}installed {len(inst)}{C.R}: {', '.join(inst)}")
    if skip:
        print(f"  {C.DIM}skipped {len(skip)} (already installed): "
              f"{', '.join(skip)}{C.R}")
    if errs:
        print(f"  {C.RED}errors:{C.R}")
        for name, msg in errs:
            print(f"    {C.RED}{name}: {msg}{C.R}")
    if inst:
        print(f"  {C.YELLOW}all installed skills are quarantined.{C.R} "
              f"review with /skills, then /promote <name> trusted-supervised")
    logger.write({
        "ts": logger.now_iso(),
        "type": "bundled_install",
        "installed": inst,
        "skipped": skip,
        "errors": [name for name, _ in errs],
    })
    return True


def _cmd_promote(arg):
    parts = arg.split()
    if len(parts) != 2:
        print(f"  {C.RED}usage: /promote <name> <state>{C.R}"); return True
    try:
        s = skills.promote(parts[0], parts[1])
    except skills.PromotionError as e:
        print(f"  {C.RED}{e}{C.R}"); return True
    print(f"  {s.name} -> {C.GREEN}{s.state}{C.R}")
    logger.write({"ts": logger.now_iso(), "type": "skill_promote",
                  "skill": s.name, "new_state": s.state})
    return True


def _cmd_skill_new(arg):
    parts = arg.strip().split(maxsplit=1)
    if not parts:
        print(f"  {C.RED}usage: /skill new | /skill review <name> | /skill import <source>{C.R}")
        return True
    sub = parts[0]
    if sub == "new":
        return _cmd_skill_draft()
    if sub == "review":
        if len(parts) < 2 or not parts[1].strip():
            print(f"  {C.RED}usage: /skill review <name>{C.R}")
            return True
        return _cmd_skill_review(parts[1].strip())
    if sub == "import":
        if len(parts) < 2 or not parts[1].strip():
            print(f"  {C.RED}usage: /skill import <path-or-url>{C.R}")
            return True
        return _cmd_skill_import(parts[1].strip())
    print(f"  {C.RED}usage: /skill new | /skill review <name> | /skill import <source>{C.R}")
    return True


def _cmd_skill_import(source):
    print(f"  {C.DIM}importing skill from {source}...{C.R}")
    try:
        path = skills_market.import_skill(source)
    except Exception as e:
        print(f"  {C.RED}import failed: {type(e).__name__}: {e}{C.R}")
        return True
    print(f"  {C.GREEN}imported{C.R} -> {path}")
    print(f"  {C.YELLOW}skill is quarantined.{C.R} review with /skills, then "
          f"/promote {path.stem if path.suffix == '.md' else path.parent.name} trusted-supervised")
    # Phase 18: if a similarly-named skill exists, show what changed.
    try:
        neighbor = skills_market.diff_against_neighbor(path)
    except Exception:
        neighbor = None
    if neighbor:
        print()
        print(f"  {C.DIM}--- diff vs closest installed skill ---{C.R}")
        for line in neighbor.splitlines():
            print(f"  {line}")
    logger.write({
        "ts": logger.now_iso(),
        "type": "skill_import",
        "source": source,
        "path": str(path),
    })
    return True


def _cmd_mcp(arg):
    parts = arg.strip().split(maxsplit=1)
    sub = parts[0] if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub == "list":
        return _cmd_mcp_list()
    if sub == "connect":
        if not rest:
            print(f"  {C.RED}usage: /mcp connect <server>{C.R}")
            return True
        return _cmd_mcp_connect(rest)
    if sub == "disconnect":
        if not rest:
            print(f"  {C.RED}usage: /mcp disconnect <server>{C.R}")
            return True
        return _cmd_mcp_disconnect(rest)
    print(f"  {C.RED}usage: /mcp list | /mcp connect <server> | /mcp disconnect <server>{C.R}")
    print(f"  {C.DIM}configure servers in {config.MCP_SERVERS_FILE} or {config.CLAUDE_SETTINGS_FILE}{C.R}")
    return True


def _cmd_mcp_list():
    servers = mcp_client.load_servers()
    active = mcp_client.get_active_clients()
    if not servers and not active:
        print(f"  {C.DIM}no MCP servers configured. drop a JSON config at "
              f"{config.MCP_SERVERS_FILE} or use ~/.claude/settings.json{C.R}")
        return True
    print()
    for name, cfg in servers.items():
        status = "connected" if name in active else "configured"
        color = C.GREEN if name in active else C.DIM
        print(f"  {C.BOLD}{name}{C.R}  {color}[{status}]{C.R}  command={cfg.command} args={cfg.args}")
    for name in active:
        if name not in servers:
            print(f"  {C.BOLD}{name}{C.R}  {C.GREEN}[connected, not in config]{C.R}")
    return True


def _cmd_mcp_connect(name):
    servers = mcp_client.load_servers()
    cfg = servers.get(name)
    if cfg is None:
        print(f"  {C.RED}no MCP server '{name}' in config{C.R}")
        return True
    if not cfg.enabled:
        print(f"  {C.YELLOW}server '{name}' is disabled in config{C.R}")
        return True
    print(f"  {C.DIM}spawning '{name}' ({cfg.command} {' '.join(cfg.args)})...{C.R}")
    try:
        client = mcp_client.connect_server(cfg)
        tools = client.list_tools()
    except Exception as e:
        print(f"  {C.RED}connect failed: {type(e).__name__}: {e}{C.R}")
        return True
    mcp_client.register_client(name, client)
    print(f"  {C.GREEN}connected{C.R} '{name}' — {len(tools)} tool(s) mounted as mcp_{name}_*")
    for t in tools:
        print(f"    - {t.get('name', '?')}")
    logger.write({
        "ts": logger.now_iso(),
        "type": "mcp_connect",
        "server": name,
        "tool_count": len(tools),
    })
    return True


def _cmd_mcp_disconnect(name):
    found = mcp_client.unregister_client(name)
    if not found:
        print(f"  {C.YELLOW}server '{name}' was not connected{C.R}")
    else:
        print(f"  {C.GREEN}disconnected{C.R} '{name}'")
        logger.write({
            "ts": logger.now_iso(),
            "type": "mcp_disconnect",
            "server": name,
        })
    return True


def _cmd_skill_draft():
    try:
        pattern = input("what pattern do you want to capture? ").strip()
    except (EOFError, KeyboardInterrupt):
        return True
    if not pattern:
        return True
    recent = logger.read_all()[-20:]
    print(f"  {C.DIM}drafting against last {len(recent)} log entries...{C.R}")
    try:
        draft = skills.draft_skill_from_log(pattern, recent)
    except Exception as e:
        print(f"  {C.RED}draft failed: {e}{C.R}"); return True
    print(f"\n  draft name: {draft.get('name')}")
    print(f"  description: {draft.get('description')}")
    print(f"  capabilities: {draft.get('capabilities')}")
    body = str(draft.get("body") or "").strip()
    print(f"  body:\n{body[:1000]}")
    try:
        ans = input("save as quarantined skill? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return True
    if ans in ("y", "yes"):
        path = skills.write_draft(draft)
        print(f"  {C.GREEN}wrote{C.R} {path}")
    return True


def _cmd_skill_review(name):
    skill = skills.load(name)
    if skill is None:
        print(f"  {C.RED}no skill named '{name}'{C.R}")
        return True
    print(f"  {C.DIM}reviewing skill '{name}' (runs={skill.runs}, "
          f"success={skill.success}, fail={skill.fail})...{C.R}")
    try:
        revision = skill_evolution.propose_revision(skill)
    except Exception as e:
        print(f"  {C.RED}propose failed: {type(e).__name__}: {e}{C.R}")
        return True
    print()
    for line in skill_evolution.render_revision(skill, revision).splitlines():
        print(f"  {line}")
    if not revision.get("changed"):
        return True
    try:
        ans = input("\napply this revision? [y/N]: ").strip().lower()
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
        print(f"  {C.GREEN}applied{C.R} -> {skill.path}")
    return True


def _cmd_eval(arg):
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
                print(f"  {C.RED}usage: /eval [--last N] [--skill <name>]{C.R}")
                return True
        if tok == "--skill":
            try:
                skill_filter = tokens[i + 1]; i += 2; continue
            except IndexError:
                print(f"  {C.RED}usage: /eval [--last N] [--skill <name>]{C.R}")
                return True
        i += 1
    suffix = f" (skill={skill_filter})" if skill_filter else ""
    print(f"  {C.DIM}replaying last {last_n} records at temp=0{suffix}...{C.R}")
    try:
        report = eval_mod.replay(last_n=last_n, skill_filter=skill_filter)
        print(eval_mod.render_summary(report))
    except Exception as e:
        print(f"  {C.RED}eval failed: {e}{C.R}")
    return True


def _cmd_plan(arg):
    v = arg.strip().lower()
    if v in ("on", "true", "1"):
        _RUN_STATE["plan"] = True; print(f"  {C.GREEN}plan-tree mode ON{C.R}")
    elif v in ("off", "false", "0"):
        _RUN_STATE["plan"] = False; print(f"  {C.YELLOW}plan-tree mode OFF{C.R}")
    else:
        print(f"  plan-tree mode: {'on' if _RUN_STATE['plan'] else 'off'}")
    return True


def _cmd_parallel(arg):
    v = arg.strip().lower()
    if v in ("on", "true", "1"):
        _RUN_STATE["parallel"] = True
        print(f"  {C.GREEN}subagent parallel mode ON{C.R} "
              f"(concurrency={config.SUBAGENT_CONCURRENCY})")
    elif v in ("off", "false", "0"):
        _RUN_STATE["parallel"] = False
        print(f"  {C.YELLOW}subagent parallel mode OFF{C.R}")
    else:
        print(f"  subagent parallel mode: "
              f"{'on' if _RUN_STATE.get('parallel') else 'off'}  "
              f"(only effective when /plan is on; cap={config.SUBAGENT_CONCURRENCY})")
    return True


def _cmd_cost():
    print()
    for line in cost.render_summary().splitlines():
        print(f"  {line}")
    return True


def _cmd_clear():
    conv = _RUN_STATE.get("conv")
    if conv is not None:
        conv.clear_turns()
        try:
            conversation.save(conv)
        except Exception:
            pass
    cost.reset_session()
    print(f"  {C.GREEN}cleared conversation turns + cost counters{C.R}")
    return True


def _cmd_compact():
    conv = _RUN_STATE.get("conv")
    if conv is None or not conv.turns:
        print(f"  {C.DIM}nothing to compact (empty conversation){C.R}")
        return True
    n_before = len(conv.turns)
    print(f"  {C.DIM}compacting {n_before} turns...{C.R}")
    try:
        conversation.compact(conv)
        conversation.save(conv)
    except Exception as e:
        print(f"  {C.RED}compact failed: {type(e).__name__}: {e}{C.R}")
        return True
    n_after = len(conv.turns)
    print(f"  {C.GREEN}compacted {n_before - n_after} turn(s){C.R} -> "
          f"{n_after} kept, {len(conv.summary)} char summary")
    return True


def _cmd_resume(arg):
    target = arg.strip()
    if not target:
        items = conversation.list_all()
        if not items:
            print(f"  {C.DIM}no saved conversations{C.R}")
            return True
        print()
        for item in items[:10]:
            print(f"  {C.BOLD}{item['id']}{C.R} "
                  f"{C.DIM}({item['turns']} turns, "
                  f"{item['last_updated'][:19]}){C.R}")
        print(f"\n  {C.DIM}usage: /resume <id>{C.R}")
        return True
    conv = conversation.load(target)
    if conv is None:
        print(f"  {C.RED}no conversation '{target}'{C.R}")
        return True
    _RUN_STATE["conv"] = conv
    print(f"  {C.GREEN}resumed{C.R} {conv.id} "
          f"({len(conv.turns)} turns, started {conv.started[:19]})")
    return True


def _cmd_continue():
    latest = conversation.latest()
    if latest is None:
        print(f"  {C.DIM}no prior conversation to continue{C.R}")
        return True
    _RUN_STATE["conv"] = latest
    print(f"  {C.GREEN}continuing{C.R} {latest.id} "
          f"({len(latest.turns)} turns)")
    return True


def _cmd_init():
    print(f"  {C.DIM}scanning workspace + drafting starter user.md / skills...{C.R}")
    try:
        proposal = init_codebase.propose()
    except Exception as e:
        print(f"  {C.RED}/init failed: {type(e).__name__}: {e}{C.R}")
        return True
    if proposal.get("error"):
        print(f"  {C.RED}{proposal['error']}{C.R}")
        return True
    print()
    for line in init_codebase.render(proposal).splitlines():
        print(line)
    print()

    adds = proposal.get("user_md_additions") or []
    if adds:
        try:
            ans = input("  apply user.md additions? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("y", "yes"):
            n = init_codebase.apply_user_md(adds)
            print(f"  {C.GREEN}wrote {n} section(s) to user.md{C.R}")

    for sk in proposal.get("skill_proposals") or []:
        try:
            ans = input(
                f"  install skill '{sk.get('name', '?')}' (quarantined)? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("y", "yes"):
            p = init_codebase.apply_skill(sk)
            print(f"  {C.GREEN}wrote{C.R} {p}")
    return True


def _cmd_model(arg):
    target = arg.strip()
    if not target:
        print(f"  current model: {C.BOLD}{config.MODEL}{C.R}")
        return True
    config.MODEL = target
    print(f"  {C.GREEN}model -> {target}{C.R}  "
          f"{C.DIM}(this session only; persist via JANUS_MODEL env){C.R}")
    return True


def _cmd_doctor():
    print(f"  {C.DIM}running diagnostics...{C.R}\n")
    results = doctor.run_all()
    for line in doctor.render(results, color=True).splitlines():
        print(line)
    return True


def _cmd_output_style(arg):
    target = arg.strip().lower()
    if not target:
        print(f"  current style: {C.BOLD}{_RUN_STATE.get('output_style')}{C.R}  "
              f"{C.DIM}(valid: {', '.join(output_styles.VALID)}){C.R}")
        return True
    if target not in output_styles.VALID:
        print(f"  {C.RED}unknown style: {target}{C.R}  "
              f"{C.DIM}valid: {', '.join(output_styles.VALID)}{C.R}")
        return True
    _RUN_STATE["output_style"] = target
    print(f"  {C.GREEN}output style -> {target}{C.R}")
    return True


def _cmd_list_commands():
    customs = _RUN_STATE.get("custom_commands") or {}
    if not customs:
        print(f"  {C.DIM}no custom commands. drop a .md at "
              f"{config.COMMANDS_DIR} to add one{C.R}")
        return True
    print()
    for name, cmd in sorted(customs.items()):
        desc = cmd.description or "(no description)"
        print(f"  {C.BOLD}/{name}{C.R}  {C.DIM}{desc}{C.R}  {C.DIM}[{cmd.path}]{C.R}")
    return True


def _cmd_triggers():
    from . import triggers as trg
    items = trg.list_triggers()
    if not items:
        print(f"  {C.DIM}no triggers in {config.TRIGGERS_DIR}{C.R}"); return True
    print()
    for t in items:
        print(f"  {C.BOLD}{t.name}{C.R} kind={t.kind} when={t.when} skill={t.skill or '-'} enabled={t.enabled}")
    return True


def analyze():
    # v1.0: interactions no longer always carry `interpretations`. Count any
    # record that has either a request (chat-shaped) or interpretations
    # (legacy interpretation-confirmed flow).
    records = [
        r for r in logger.read_all()
        if r.get("request") or r.get("interpretations")
    ]
    if not records:
        print("  no log yet."); return
    n = len(records)
    choices: dict = {}
    for r in records:
        key = str(r.get("choice") or "—")
        choices[key] = choices.get(key, 0) + 1
    print(f"\n  {C.BOLD}log analysis{C.R}")
    print(f"  total interactions: {n}")
    for k, v in sorted(choices.items()):
        print(f"    {k:>18}: {v:>3} ({100*v/n:.1f}%)")
    s_stats = index.stats()
    print(f"\n  {C.BOLD}index:{C.R} {s_stats['rows']} rows")
    skl = skills.list_skills()
    if skl:
        print(f"  {C.BOLD}skills:{C.R} {len(skl)}")
        for s in skl:
            print(f"    {s.name:<24} {s.state}")
    print()


def maybe_propose_memory(req, output, cache_snap=None):
    """Prompt-and-apply a memory diff. If `cache_snap` is provided and
    user.md is updated, refreshes the snapshot in place so subsequent
    turns see the new preamble (and the prompt cache invalidates for
    one turn, then re-warms)."""
    if not config.MEMORY_PROPOSE_ENABLED:
        return
    try:
        ops = memory.propose_diff(req, output)
    except Exception as e:
        print(f"  {C.DIM}memory propose skipped: {type(e).__name__}: {e}{C.R}")
        return
    if not ops:
        return
    print(f"\n  {C.BOLD}proposed memory updates{C.R}\n")
    for line in memory.render_diff(ops).splitlines():
        print(f"  {line}")
    try:
        ans = input("\napply? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans in ("y", "yes"):
        memory.apply(ops)
        print(f"  {C.GREEN}applied to user.md{C.R}")
        if cache_snap is not None:
            cache_snap.preamble = cache.snapshot().preamble


def main():
    """v1.0 main loop — Claude-Code-shaped chat with mode-gated tool use.

    No more interpretation picker. Same shape as cli_rich's main(), in
    plain ANSI for users without prompt_toolkit/rich.
    """
    config.assert_configured()
    config.ensure_home()
    banner()

    try:
        ctx = hooks.fire(hooks.SESSION_START, {"workspace": str(config.WORKSPACE)})
        if ctx.injected_context:
            print(f"{C.DIM}[SessionStart hook] {ctx.injected_context[:200]}{C.R}")
    except Exception:
        pass

    try:
        added = index.sync()
        if added:
            print(f"{C.DIM}indexed {added} new log entries{C.R}")
    except Exception as e:
        print(f"{C.DIM}index sync skipped: {e}{C.R}")

    # v1.0 mode state. Seeded from legacy JANUS_APPROVAL.
    mode_state: permissions.ModeState = _RUN_STATE["mode_state"]
    mode_state.set(permissions.normalize(config.APPROVAL_MODE))
    if mode_state.current == permissions.BYPASS:
        print(
            f"{C.RED}{C.BOLD}WARNING:{C.R} {C.RED}bypassPermissions mode active — "
            f"every tool will run without asking. /mode default to disable.{C.R}"
        )
    base_approver = make_mode_approver(mode_state)

    cache_snap = cache.snapshot()

    pending = conversation.take_pending()
    _RUN_STATE["conv"] = pending if pending is not None else conversation.new()
    conv = _RUN_STATE["conv"]
    if pending is not None:
        print(f"{C.DIM}   resumed conversation {conv.id} "
              f"({len(conv.turns)} turns){C.R}\n")
        # Rebuild messages from saved turns so the model has context.
        for t in conv.turns:
            req_t = (t.get("request") or "").strip()
            out_t = (t.get("output") or "").strip()
            if req_t:
                _RUN_STATE["messages"].append({"role": "user", "content": req_t})
            if out_t:
                _RUN_STATE["messages"].append({"role": "assistant", "content": out_t})

    try:
        _RUN_STATE["custom_commands"] = commands_mod.load_all()
        n = len(_RUN_STATE["custom_commands"])
        if n:
            print(f"{C.DIM}   loaded {n} custom command(s){C.R}\n")
    except Exception:
        _RUN_STATE["custom_commands"] = {}

    while True:
        st = statusline.render(statusline.StatusInputs(
            model=config.MODEL,
            turn=_RUN_STATE.get("turn", 0),
            plan_on=False,
            parallel_on=False,
            verbose=_RUN_STATE.get("verbose", False),
            permission_mode=mode_state.current,
            conv_turns=len(_RUN_STATE["conv"].turns) if _RUN_STATE.get("conv") else 0,
        ))
        print(f"{C.DIM}{st}{C.R}")
        try:
            req = _read_input_with_continuation(
                f" {C.BOLD}{C.GREEN}{branding.PROMPT_GLYPH}{C.R}  ",
                f"   {C.DIM}…{C.R}  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not req:
            continue
        if req.lower() in ("q", "quit", "exit"):
            break
        if handle_command(req):
            continue
        if _RUN_STATE.get("_pending_custom"):
            req = _RUN_STATE.pop("_pending_custom")

        try:
            up = hooks.fire(hooks.USER_PROMPT_SUBMIT, {"request": req})
            if not up.allow:
                print(f"  {C.YELLOW}prompt blocked by hook:{C.R} {up.reason}")
                continue
            if up.modified_args and isinstance(up.modified_args.get("request"), str):
                req = up.modified_args["request"]
                print(f"  {C.DIM}[UserPromptSubmit hook rewrote request]{C.R}")
        except Exception:
            pass

        _RUN_STATE["last_user_input"] = req

        record: dict[str, Any] = {
            "ts": logger.now_iso(), "model": config.MODEL,
            "workspace": str(config.WORKSPACE), "request": req,
            "mode": mode_state.current,
        }

        cost.new_turn()
        _RUN_STATE["turn"] = _RUN_STATE.get("turn", 0) + 1

        conv = _RUN_STATE["conv"]
        preamble = cache_snap.preamble + conv.recent_context_block()

        # Skill auto-attach: trusted-auto only.
        all_skills = skills.list_skills()
        matches = skills.match(req, all_skills)
        attached_skill = None
        for s in matches:
            if s.state == "trusted-auto":
                attached_skill = s
                print(f"{C.DIM}auto-attached skill:{C.R} {s.name}")
                break
        if attached_skill:
            record["skill"] = attached_skill.name
            record["skill_state"] = attached_skill.state
        elif matches:
            names = ", ".join(s.name for s in matches[:3])
            print(
                f"{C.DIM}matching skills (not auto):{C.R} {names} "
                f"{C.DIM}(promote one to attach automatically){C.R}"
            )

        skill_caps = attached_skill.capabilities if attached_skill else CapabilitySet()
        tools = default_registry(capabilities=skill_caps)
        approver = make_capability_aware(base_approver, skill_caps)

        try:
            t0 = time.time()
            output, trace = executor.chat(
                messages=_RUN_STATE["messages"],
                user_input=req,
                tools=tools,
                approver=approver,
                on_step=render_step,
                skill_body=(attached_skill.body if attached_skill else ""),
                memory_preamble=preamble,
                mode=mode_state.current,
                workspace=str(config.WORKSPACE),
                tool_count=len(tools.names()),
                skill_count=len(all_skills),
                stream=False,
            )
            record["execute_ms"] = int((time.time() - t0) * 1000)
            record["trace"] = trace
            record["output"] = output
        except Exception as e:
            print(f"{C.RED}executor failed:{C.R} {e}")
            record["error"] = f"execute: {e}"
            logger.write(record); continue

        # Apply output style and render.
        rendered = output_styles.render(
            output, _RUN_STATE.get("output_style", "markdown"),
        )
        out_lines = rendered.splitlines() or [""]
        max_w = min(80, max(40, max(len(l) for l in out_lines) + 4))
        bar = "─" * (max_w - 2)
        print(f"\n  {C.BOLD}{C.BLUE}┌{bar[:6]} output {bar[14:]}┐{C.R}")
        for ln in out_lines:
            print(f"  {C.BLUE}│{C.R} {ln}")
        print(f"  {C.BOLD}{C.BLUE}└{bar}┘{C.R}\n")

        fb = feedback()
        if fb:
            record["feedback"] = fb

        if attached_skill:
            success = skill_evolution.resolve_success(output, trace, fb)
            try:
                updated = skills.record_run(attached_skill.name, success=success)
            except Exception:
                updated = None
            if updated and skill_evolution.should_propose(updated):
                print(f"  {C.DIM}skill '{updated.name}' has {updated.runs} runs "
                      f"(success={updated.success}, fail={updated.fail}); "
                      f"consider /skill review {updated.name}{C.R}")
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
            print(f"  {C.DIM}({len(conv.turns)} turns this conversation; "
                  f"consider /compact){C.R}")

        try:
            ev = hooks.STOP_FAILURE if record.get("error") else hooks.STOP
            hooks.fire(ev, {"request": req, "output": output, "ts": record["ts"]})
        except Exception:
            pass

        try:
            index.sync()
        except Exception:
            pass
        maybe_propose_memory(req, output, cache_snap=cache_snap)
        print()

    n = len(logger.read_all())
    print(f"{C.DIM}{n} interactions logged at {config.LOG_FILE}{C.R}")


def _serialize_plan(node):
    return {"id": node.id, "goal": node.goal, "skill": node.skill,
            "deps": list(node.deps),
            "children": [_serialize_plan(c) for c in node.children]}


if __name__ == "__main__":
    if "--analyze" in sys.argv or "-a" in sys.argv:
        config.ensure_home()
        analyze()
    else:
        try:
            main()
        except KeyboardInterrupt:
            print()
