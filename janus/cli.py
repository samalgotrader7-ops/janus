"""cli.py -- basic terminal UI (input() loop). cli_rich provides the polished one."""
from __future__ import annotations
import sys
import time
from typing import Any

from . import config, interpreter, executor, logger, memory, index, skills
from . import eval as eval_mod, planner, orchestrator, skill_evolution
from . import skills_market, hooks, cache, branding, conversation, cost
from . import statusline, commands as commands_mod, doctor, init_codebase
from . import output_styles
from .mcp import client as mcp_client
from .tools import default_registry, make_capability_aware, CapabilitySet


class C:
    R = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    BLUE = "\033[34m"; CYAN = "\033[36m"; GREEN = "\033[32m"
    YELLOW = "\033[33m"; RED = "\033[31m"; MAGENTA = "\033[35m"


_RUN_STATE: dict = {
    "plan": False, "parallel": False, "conv": None,
    "verbose": False, "turn": 0,
    "output_style": config.OUTPUT_STYLE,
    "custom_commands": {},  # populated at session start
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


def make_approver():
    mode = config.APPROVAL_MODE
    def approver(label, details):
        if mode == "auto":
            print(f"{C.DIM}[auto] {label}{C.R}"); return True
        if mode == "dry-run":
            print(f"{C.YELLOW}[dry-run] {label}{C.R}\n  {details}"); return False
        print(f"\n{C.YELLOW}[approval] {C.BOLD}{label}{C.R}")
        for line in details.splitlines():
            print(f"  {line}")
        try:
            return input("approve? [y/N]: ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False
    return approver


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

    if cmd == "/workspace":
        return _cmd_workspace(arg)
    if cmd == "/analyze":
        analyze(); return True
    if cmd == "/memory":
        return _cmd_memory()
    if cmd == "/search":
        return _cmd_search(arg)
    if cmd == "/skills":
        return _cmd_skills()
    if cmd == "/promote":
        return _cmd_promote(arg)
    if cmd == "/skill":
        return _cmd_skill_new(arg)
    if cmd == "/eval":
        return _cmd_eval(arg)
    if cmd == "/plan":
        return _cmd_plan(arg)
    if cmd == "/parallel":
        return _cmd_parallel(arg)
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
        print("/workspace /memory /search /skills /promote /skill {new|review|import} /cost /clear /compact /resume [id] /continue /eval [--last N] [--skill <n>] /plan /parallel /mcp {list|connect|disconnect} /triggers /analyze q")
        return True
    print(f"  {C.RED}unknown: {cmd}{C.R}")
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


def _cmd_memory():
    txt = memory.read()
    if not txt:
        print(f"  {C.DIM}(no user.md yet){C.R}")
    else:
        print(); print(txt)
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


def _cmd_skills():
    items = skills.list_skills()
    if not items:
        print(f"  {C.DIM}no skills yet -- /skill new{C.R}"); return True
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
    records = [r for r in logger.read_all() if r.get("interpretations")]
    if not records:
        print("  no log yet."); return
    n = len(records)
    choices: dict = {}
    for r in records:
        choices[str(r.get("choice"))] = choices.get(str(r.get("choice")), 0) + 1
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
    config.assert_configured()
    config.ensure_home()
    banner()

    # Phase 11: SessionStart hook (observation; injected_context is logged).
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

    base_approver = make_approver()

    # Phase 12: snapshot the memory preamble at session start. Reused below
    # so the leading prompt bytes are byte-identical across turns and the
    # provider's prompt cache hits.
    cache_snap = cache.snapshot()

    # Phase 13: bind a conversation. __main__ may have stashed one via
    # --continue / --resume; otherwise start fresh.
    pending = conversation.take_pending()
    _RUN_STATE["conv"] = pending if pending is not None else conversation.new()
    conv = _RUN_STATE["conv"]
    if pending is not None:
        print(f"{C.DIM}   resumed conversation {conv.id} "
              f"({len(conv.turns)} turns){C.R}\n")

    # Phase 15: load user-defined slash commands from disk.
    try:
        _RUN_STATE["custom_commands"] = commands_mod.load_all()
        n = len(_RUN_STATE["custom_commands"])
        if n:
            print(f"{C.DIM}   loaded {n} custom command(s){C.R}\n")
    except Exception:
        _RUN_STATE["custom_commands"] = {}

    while True:
        # Phase 14: status line above the prompt — model, tokens, mode flags.
        st = statusline.render(statusline.StatusInputs(
            model=config.MODEL,
            turn=_RUN_STATE.get("turn", 0),
            plan_on=_RUN_STATE.get("plan", False),
            parallel_on=_RUN_STATE.get("parallel", False),
            verbose=_RUN_STATE.get("verbose", False),
            permission_mode=config.APPROVAL_MODE,
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
        # Phase 15: a custom slash command stashed a rewritten request.
        if _RUN_STATE.get("_pending_custom"):
            req = _RUN_STATE.pop("_pending_custom")
            print(f"{C.DIM}   /{req[:60]}...{C.R}" if len(req) > 60 else "")

        # Phase 11: UserPromptSubmit hook can deny or rewrite the request.
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

        record: dict[str, Any] = {
            "ts": logger.now_iso(), "model": config.MODEL,
            "workspace": str(config.WORKSPACE), "request": req,
        }

        # Phase 13: reset per-turn cost counters at the top of each request.
        cost.new_turn()
        # Phase 14: per-turn counter for the status line.
        _RUN_STATE["turn"] = _RUN_STATE.get("turn", 0) + 1

        # Phase 12+13: cache snapshot is the long-term memory; the conversation
        # recap is per-turn (it changes each turn) so we concatenate without
        # mutating the snapshot.
        conv = _RUN_STATE["conv"]
        preamble = cache_snap.preamble + conv.recent_context_block()
        all_skills = skills.list_skills()
        matches = skills.match(req, all_skills)
        skill_hints = "\n".join(f"- {s.name} ({s.state}): {s.description}" for s in matches[:5])

        try:
            t0 = time.time()
            interps = interpreter.interpret(req, memory_preamble=preamble, skill_hints=skill_hints)
            record["interpret_ms"] = int((time.time() - t0) * 1000)
            record["interpretations"] = interps
        except Exception as e:
            print(f"{C.RED}interpreter failed:{C.R} {e}")
            record["error"] = f"interpret: {e}"
            logger.write(record); continue

        if len(interps) == 1:
            print(f"{C.DIM}(unambiguous){C.R}")
            show_interpretations(interps)
            chosen = interps[0]; record["choice"] = "auto-single"
        else:
            show_interpretations(interps)
            ch = prompt_choice(len(interps))
            if ch == "q":
                logger.write(record); break
            if ch == "r":
                try:
                    correction = input("what did you actually mean: ").strip()
                except (EOFError, KeyboardInterrupt):
                    logger.write(record); continue
                record["choice"] = "refine"; record["correction"] = correction
                chosen = {"label": "user-corrected", "action": correction, "risk": ""}
            elif ch == "s":
                record["choice"] = "skip"
                chosen = {"label": "skip-clarification", "action": req, "risk": ""}
            else:
                record["choice"] = ch
                chosen = interps[ch - 1]

        attached_skill = prompt_skill_attach(matches) if matches else None
        if attached_skill:
            record["skill"] = attached_skill.name
            record["skill_state"] = attached_skill.state

        plan_tree = None
        if _RUN_STATE.get("plan"):
            try:
                print(f"{C.DIM}planning...{C.R}")
                plan_tree = planner.plan(chosen["action"],
                                         available_skills=[s.name for s in all_skills])
                print(planner.render(plan_tree))
                record["plan"] = _serialize_plan(plan_tree)
            except Exception as e:
                print(f"{C.RED}planner failed: {e}{C.R}"); plan_tree = None

        skill_caps = attached_skill.capabilities if attached_skill else CapabilitySet()
        tools = default_registry(capabilities=skill_caps)
        approver = make_capability_aware(base_approver, skill_caps)

        print(f"\n{C.DIM}   ┄ executing ┄{C.R}")
        try:
            t0 = time.time()
            if plan_tree is not None:
                rr = orchestrator.run(
                    original_request=req, chosen_label=chosen["label"],
                    chosen_action=chosen["action"], plan=plan_tree,
                    base_approver=base_approver, on_step=render_step,
                    on_leaf_start=lambda n: print(f"   {C.BOLD}{branding.LEAF_START} leaf {n.id}{C.R} {n.goal}"),
                    on_leaf_done=lambda lr: print(
                        f"   {C.GREEN if not lr.error else C.RED}{branding.TOOL_OK if not lr.error else branding.TOOL_FAIL} {lr.id}{C.R}"),
                    memory_preamble=preamble, attached_skill=attached_skill,
                    parallel=_RUN_STATE.get("parallel", False),
                    parent_id=record["ts"],
                )
                output = rr.final_output
                trace = [{"leaf": lr.id, "trace": lr.trace, "error": lr.error}
                         for lr in rr.leaves]
            else:
                output, trace = executor.execute(
                    original_request=req, chosen_label=chosen["label"],
                    chosen_action=chosen["action"], tools=tools, approver=approver,
                    on_step=render_step,
                    skill_body=(attached_skill.body if attached_skill else ""),
                    memory_preamble=preamble,
                )
            record["execute_ms"] = int((time.time() - t0) * 1000)
            record["trace"] = trace
            record["output"] = output
        except Exception as e:
            print(f"{C.RED}executor failed:{C.R} {e}")
            record["error"] = f"execute: {e}"
            logger.write(record); continue

        # Phase 15: apply output style (markdown/plain/terse/json).
        output = output_styles.render(
            output, _RUN_STATE.get("output_style", "markdown"),
        )

        # Output panel — bordered for visual clarity (mockup §design).
        out_lines = output.splitlines() or [""]
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

        # Phase 13: append to the conversation + persist for --continue.
        try:
            conv.add_turn(
                request=req, output=output,
                choice=record.get("choice"),
                skill=(attached_skill.name if attached_skill else None),
                ts=record.get("ts"),
            )
            conversation.save(conv)
        except Exception:
            pass

        # If the conversation got long enough, hint to compact (don't auto).
        if len(conv.turns) >= config.COMPACT_THRESHOLD_TURNS:
            print(f"  {C.DIM}({len(conv.turns)} turns this conversation; "
                  f"consider /compact){C.R}")

        # Phase 11: Stop / StopFailure hook fires after each turn.
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

    n = len([r for r in logger.read_all() if r.get("interpretations")])
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
