"""Entry point: python -m janus [subcommand] [flags].

  chat                          default -- interactive REPL (rich if available)
  telegram                      run the Telegram gateway (needs JANUS_TELEGRAM_TOKEN)
  pair list                     list pending pairing-code requests across gateways
  pair approve <CODE>           approve a pairing code (owner authorization)
  pair revoke <gateway> <id>    remove a previously-approved chat
  pair approved                 list all approved (gateway, chat_id) pairs
  uninstall [--yes] [--dry-run] inventory and remove ~/.janus/ state directory
  swarm list                    list installed specs and recent runs
  swarm describe <name>         show one spec's details
  swarm run <name> [--background] [k=v ...]   launch a swarm; --background detaches
  swarm status <run-id>         show current state of a swarm run
  swarm cancel <run-id>         write cancel.flag (cooperative cancellation)
  swarm cost <run-id>           per-swarm cost breakdown
  swarm trace <run-id>          replay timeline.jsonl for a run
  daemon                        run the proactive trigger daemon
  daemon --once                 single iteration of the daemon loop (cron/systemd)
  fire <trigger>                fire one named trigger immediately
  web [--host H] [--port P]     run the local web UI (FastAPI; 127.0.0.1 by default)
  whatsapp [--port P]           run the WhatsApp Cloud API webhook gateway
  --basic                       force the basic CLI even if rich is installed
  --analyze | -a                print log statistics and exit
  --reindex                     rebuild ~/.janus/sessions.db
  --eval [--last N]             replay recent log entries
  --eval --skill <name>         replay only records that used <name>
  --eval --drift-budget X       exit non-zero if avg interp_drift > X (CI gate)
  --eval --output-format json   emit the full report as JSON on stdout
  --version, -V                 print the version string and exit
  --logo                        print the ASCII logo + version (pipeable)
  --logo --svg                  print the SVG markup
  --logo --plain                print the bare logo lines (no version/tagline)
  --continue                    resume the most recent conversation
  --resume <id>                 resume a specific conversation (id = filename stem)
  --conversations               list saved conversations and exit

  -p, --prompt <text>           headless: run one prompt and exit (or pipe stdin)
  --output-format <fmt>         text | json | jsonl  (default text)
  --skill <name>                attach a skill in headless mode
  --no-color                    strip ANSI from output
  --quiet                       suppress non-output lines
"""

import json as _json
import os as _os
import sys

# Windows: console default is cp1252 which can't render the ╱─► / ┄ / ► /
# box-drawing chars in branding.py. Reconfigure to utf-8 once at entry so
# the banner doesn't crash. No-op on Linux/macOS where utf-8 is default.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def _check_locale_and_warn() -> None:
    """v1.24.3: warn once at startup when the terminal locale will
    misrender emoji. Sam ran into this on his Ubuntu deploy under
    tmux — emojis came out as 'Ã°ÂÂÂ' (UTF-8 bytes interpreted as
    Latin-1 and doubly encoded along the way).

    We can't fix the terminal from here, but we can:
      (a) tell the user how to fix it
      (b) auto-fall-back to ASCII glyphs in the meantime
    """
    if _os.environ.get("JANUS_LOCALE_QUIET", "").lower() in (
        "1", "true", "yes",
    ):
        return
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    lang = (
        _os.environ.get("LC_ALL")
        or _os.environ.get("LC_CTYPE")
        or _os.environ.get("LANG")
        or ""
    )
    looks_bad = ("utf" not in enc) or (
        lang.lower() in ("", "c", "posix", "ansi_x3.4-1968")
    )
    if not looks_bad:
        return
    # Print to stderr so it doesn't pollute -p output piping.
    msg = (
        f"\033[33m[janus] terminal locale looks unsafe for emoji "
        f"rendering (LANG={lang or '<empty>'}, encoding={enc or '?'}).\033[0m\n"
        f"\033[33m         emojis will be replaced with ASCII fallbacks. "
        f"to enable emoji output:\033[0m\n"
        f"\033[33m         export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8\033[0m\n"
        f"\033[33m         (silence this warning with JANUS_LOCALE_QUIET=1)\033[0m\n"
    )
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass


_check_locale_and_warn()

from . import config, index, eval as eval_mod  # noqa: E402  (after stdout fix)


def _stash_conversation_from_flags(args):
    """If --continue or --resume <id> is on the command line, load the
    conversation and stash it for cli/cli_rich to pick up."""
    from . import conversation
    if "--continue" in args:
        conv = conversation.latest()
        if conv is None:
            print("error: no prior conversation to continue"); sys.exit(2)
        conversation.set_pending(conv); return
    if "--resume" in args:
        i = args.index("--resume")
        try:
            target = args[i + 1]
        except IndexError:
            print("error: --resume requires an id"); sys.exit(2)
        conv = conversation.load(target)
        if conv is None:
            print(f"error: no conversation '{target}'"); sys.exit(2)
        conversation.set_pending(conv)


def _maybe_install_bundled_skills():
    """First-run hook: copy bundled skills into ~/.janus/skills/ if empty.

    Suppressed by JANUS_NO_BUNDLED=1. Quiet by default; only prints if
    something was installed. Failures never abort launch (P8).
    """
    import os
    if os.environ.get("JANUS_NO_BUNDLED", "").lower() in ("1", "true", "yes"):
        return
    try:
        from . import skill_catalog
        if not skill_catalog.is_first_run():
            return
        result = skill_catalog.install_bundled()
        n = len(result["installed"])
        if n:
            sys.stderr.write(
                f"janus: installed {n} bundled skill(s) (all quarantined). "
                f"Run /skills to browse.\n"
            )
    except Exception:
        pass


def _run_chat():
    _stash_conversation_from_flags(sys.argv[1:])
    _maybe_install_bundled_skills()
    if "--basic" in sys.argv:
        from .cli import main; main(); return
    try:
        from .cli_rich import main as rich_main
        rich_main()
    except ImportError:
        from .cli import main
        main()


def _run_headless(args):
    """`-p` / `--prompt` mode (or stdin pipe)."""
    from . import headless

    prompt = ""
    for flag in ("-p", "--prompt"):
        if flag in args:
            i = args.index(flag)
            try:
                prompt = args[i + 1]
                # If `-p` was given without an arg AND stdin is piped,
                # fall through to stdin read below.
                if prompt.startswith("-"):
                    prompt = ""
            except IndexError:
                prompt = ""
            break

    # If still no prompt and stdin is piped, read it.
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read()

    output_format = "text"
    if "--output-format" in args:
        i = args.index("--output-format")
        try:
            output_format = args[i + 1]
        except IndexError:
            print("error: --output-format requires a value (text|json|jsonl)")
            sys.exit(2)

    skill_name = None
    if "--skill" in args:
        i = args.index("--skill")
        try:
            skill_name = args[i + 1]
        except IndexError:
            print("error: --skill requires a name"); sys.exit(2)

    no_color = "--no-color" in args
    quiet = "--quiet" in args

    _stash_conversation_from_flags(args)

    rc = headless.run(
        prompt=prompt,
        output_format=output_format,
        no_color=no_color,
        quiet=quiet,
        skill_name=skill_name,
    )
    sys.exit(rc)


def _is_headless_invocation(args: list[str]) -> bool:
    """`-p` / `--prompt` flag, OR stdin is piped (not a tty)."""
    if "-p" in args or "--prompt" in args:
        return True
    if not sys.stdin.isatty():
        # Don't accidentally consume stdin for `--logo` etc.
        # Only headless when no other subcommand was given OR only flags
        # that fit headless mode are present.
        non_pipe_subs = {
            "chat", "--chat", "telegram", "web", "whatsapp", "daemon",
            "fire", "--analyze", "-a", "--reindex", "--eval",
            "--logo", "--conversations", "--help", "-h", "help",
            "insights", "stats", "memory", "onboard", "service", "swarm", "pair", "uninstall",
        }
        if not args or all(a not in non_pipe_subs for a in args):
            return True
    return False


def _list_conversations():
    from . import conversation
    items = conversation.list_all()
    if not items:
        print("(no saved conversations)")
        return
    for it in items:
        print(f"{it['id']}  turns={it['turns']:>3}  updated={it['last_updated'][:19]}  model={it['model']}")


def _run_telegram():
    from .gateways.telegram import serve
    serve()


def _run_web(args):
    # v1.21: subcommands. `janus web` (no args) starts the server.
    # `janus web rotate-token` regenerates the bootstrap token.
    # v1.33.0: `janus web config <proxy>` emits a reverse-proxy
    # snippet (Caddy / nginx) for production deployment.
    if args and args[0] == "rotate-token":
        from .gateways.web import rotate_token_cmd
        sys.exit(rotate_token_cmd())
    if args and args[0] == "config":
        from .web_config import cmd_config
        sys.exit(cmd_config(args[1:]))
    from .gateways.web import serve
    host = None
    port = None
    if "--host" in args:
        try:
            host = args[args.index("--host") + 1]
        except IndexError:
            print("error: --host requires a value"); sys.exit(2)
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
        except (IndexError, ValueError):
            print("error: --port requires an integer"); sys.exit(2)
    sys.exit(serve(host=host, port=port))


def _run_whatsapp(args):
    from .gateways.whatsapp import serve
    host = "127.0.0.1"
    port = 8766
    if "--host" in args:
        try:
            host = args[args.index("--host") + 1]
        except IndexError:
            print("error: --host requires a value"); sys.exit(2)
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
        except (IndexError, ValueError):
            print("error: --port requires an integer"); sys.exit(2)
    sys.exit(serve(host=host, port=port))


def _run_daemon(once=False):
    from .triggers import run_daemon
    run_daemon(once=once)


def _run_pair(args):
    """Owner CLI for the gateway pairing system (v1.3).

    Subcommands:
      list                     show pending code requests
      approved                 show approved (gateway, chat_id) pairs
      approve <CODE>           authorize the chat that requested CODE
      revoke <gateway> <chat>  remove a previously-approved chat
    """
    from .gateways import _common as gw
    config.ensure_home()
    sub = args[0] if args else "list"
    if sub == "list":
        pending = gw.list_pending()
        if not pending:
            print("(no pending pairing requests)"); return
        for p in pending:
            label = f" — {p.user_label}" if p.user_label else ""
            print(f"{p.code}  {p.gateway:>10}  chat={p.chat_id}{label}  "
                  f"requested={p.created_at}")
        return
    if sub == "approved":
        approved = gw.list_approved()
        if not approved:
            print("(no approved chats)"); return
        for gateway, ids in sorted(approved.items()):
            print(f"{gateway}:")
            for cid in ids:
                print(f"  {cid}")
        return
    if sub == "approve":
        if len(args) < 2:
            print("usage: janus pair approve <CODE>"); sys.exit(2)
        pc = gw.approve_code(args[1])
        if pc is None:
            print(f"error: code {args[1]} unknown or expired"); sys.exit(2)
        label = f" ({pc.user_label})" if pc.user_label else ""
        print(f"approved: {pc.gateway} chat={pc.chat_id}{label}")
        return
    if sub == "revoke":
        if len(args) < 3:
            print("usage: janus pair revoke <gateway> <chat_id>"); sys.exit(2)
        if gw.revoke(args[1], args[2]):
            print(f"revoked: {args[1]} chat={args[2]}")
        else:
            print(f"not found: {args[1]} chat={args[2]}"); sys.exit(1)
        return
    print(f"unknown pair subcommand: {sub}")
    print("usage: janus pair {list|approved|approve <code>|revoke <gw> <chat>}")
    sys.exit(2)


def _run_uninstall(args):
    """`janus uninstall [--yes] [--dry-run]` — remove ~/.janus/ state.

    Interactively inventories what will be removed, requires explicit
    'yes' confirmation, then `shutil.rmtree`s the home directory. Does
    NOT remove the pipx package — Python can't reliably uninstall its
    own running interpreter — so we print the `pipx uninstall` command
    as the next step.

    Flags:
      --yes       skip confirmation (for scripts)
      --dry-run   show inventory without removing anything

    Honors $JANUS_HOME if set; if you have a custom JANUS_HOME, the
    inventory shows that path so you know exactly what's being removed.
    """
    import shutil

    yes = "--yes" in args or "-y" in args
    dry = "--dry-run" in args or "-n" in args

    home = config.HOME
    if not home.is_dir():
        print(f"(no Janus state at {home} — nothing to uninstall)")
        print("\nTo remove the package: pipx uninstall janus-agent")
        return

    print(f"Janus state directory: {home}")
    print()
    inventory = _inventory_home(home)
    if not inventory:
        print("  (directory exists but is empty)")
    else:
        for line in inventory:
            print(f"  {line}")
    print()
    total_bytes = _dir_size(home)
    print(f"Total: {_format_bytes(total_bytes)}")
    print()
    print("This does NOT remove the package itself. To finish:")
    print("  pipx uninstall janus-agent")
    print()

    if dry:
        print("(--dry-run: nothing removed)")
        return

    if not yes:
        try:
            ans = input(f"Type 'yes' to remove {home}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print("aborted.")
            sys.exit(1)
        if ans != "yes":
            print("aborted.")
            sys.exit(1)

    try:
        shutil.rmtree(home)
    except OSError as e:
        print(f"error removing {home}: {e}")
        sys.exit(1)
    print(f"removed: {home}")
    print()
    print("To remove the package:  pipx uninstall janus-agent")


def _inventory_home(home):
    """Return a list of human-readable lines describing what's under HOME.

    Tolerates missing/empty subdirs — every category is optional and
    zero-count lines are omitted to avoid noise. Does NOT recurse into
    skill directories (just counts top-level entries).
    """
    lines = []
    skills = home / "skills"
    if skills.is_dir():
        n = len(list(skills.glob("*.md"))) + len(list(skills.glob("*/SKILL.md")))
        if n > 0:
            lines.append(f"skills/        {n} skill(s)")
    memory = home / "memory"
    if memory.is_dir():
        n = len(list(memory.glob("*.md")))
        if n > 0:
            lines.append(f"memory/        {n} category file(s)")
    convos = home / "conversations"
    if convos.is_dir():
        n = len(list(convos.glob("*.json")))
        if n > 0:
            lines.append(f"conversations/ {n} saved conversation(s)")
    sessions = home / "sessions"
    if sessions.is_dir():
        total = sum(1 for _ in sessions.rglob("*.json"))
        gw_dirs = [p.name for p in sessions.iterdir() if p.is_dir()]
        if total > 0:
            lines.append(
                f"sessions/      {total} session(s) across {len(gw_dirs)} gateway(s) "
                f"({', '.join(sorted(gw_dirs))})"
            )
    pairing = home / "pairing"
    if pairing.is_dir():
        approved_path = pairing / "approved.json"
        pending_path = pairing / "pending.json"
        a = _safe_count(approved_path, lambda d: sum(len(v or []) for v in d.values()))
        p = _safe_count(pending_path, len)
        if a + p > 0:
            lines.append(f"pairing/       {a} approved chat(s), {p} pending code(s)")
    log = home / "log.jsonl"
    if log.is_file():
        lines.append(f"log.jsonl      {_format_bytes(log.stat().st_size)} of audit log")
    cost = home / "cost.jsonl"
    if cost.is_file():
        n = sum(1 for _ in cost.open(encoding="utf-8")) if cost.is_file() else 0
        lines.append(f"cost.jsonl     {n} ledger entries ({_format_bytes(cost.stat().st_size)})")
    user_md = home / "user.md"
    if user_md.is_file():
        lines.append(f"user.md        legacy memory file ({_format_bytes(user_md.stat().st_size)})")
    home_channels = home / "home_channels.json"
    if home_channels.is_file():
        lines.append("home_channels.json  per-gateway home channel registry")
    identities = home / "identities.json"
    if identities.is_file():
        lines.append("identities.json     cross-platform identity links")
    mcp = home / "mcp"
    if mcp.is_dir() and any(mcp.iterdir()):
        lines.append("mcp/           MCP server configuration")
    hooks = home / "hooks.json"
    if hooks.is_file():
        lines.append("hooks.json     gateway lifecycle hooks")
    triggers = home / "triggers"
    if triggers.is_dir() and any(triggers.iterdir()):
        n = len(list(triggers.glob("*.yaml"))) + len(list(triggers.glob("*.yml")))
        lines.append(f"triggers/      {n} proactive trigger(s)")
    return lines


def _safe_count(path, counter):
    import json
    try:
        return counter(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0


def _dir_size(path):
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _format_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fire(name):
    from .triggers import load_triggers, fire_once
    config.ensure_home()
    triggers = load_triggers()
    t = triggers.get(name)
    if t is None:
        print(f"no trigger {name!r}. available: {', '.join(sorted(triggers))}")
        sys.exit(1)
    print(fire_once(t))


def _run_eval():
    """`--eval` with optional CI gating via `--drift-budget`.

    Exit codes:
      0 ok
      2 usage error
      5 drift budget exceeded (CI gate)
    """
    import json
    config.ensure_home()
    last_n = config.EVAL_DEFAULT_LAST
    if "--last" in sys.argv:
        i = sys.argv.index("--last")
        try:
            last_n = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            pass
    skill_filter: str | None = None
    if "--skill" in sys.argv:
        i = sys.argv.index("--skill")
        try:
            skill_filter = sys.argv[i + 1]
        except IndexError:
            print("error: --skill requires a name"); sys.exit(2)

    drift_budget: float | None = None
    if "--drift-budget" in sys.argv:
        i = sys.argv.index("--drift-budget")
        try:
            drift_budget = float(sys.argv[i + 1])
        except (IndexError, ValueError):
            print("error: --drift-budget requires a float"); sys.exit(2)

    output_format = "text"
    if "--output-format" in sys.argv:
        i = sys.argv.index("--output-format")
        try:
            output_format = sys.argv[i + 1]
        except IndexError:
            output_format = "text"
        if output_format not in ("text", "json"):
            print(f"error: unknown --output-format: {output_format}"); sys.exit(2)

    report = eval_mod.replay(last_n=last_n, skill_filter=skill_filter)

    if output_format == "json":
        sys.stdout.write(json.dumps(report.to_json(), ensure_ascii=False) + "\n")
    else:
        if skill_filter:
            print(f"  filtered to skill: {skill_filter}")
        print(eval_mod.render_summary(report))

    if drift_budget is not None:
        if report.n_records == 0:
            sys.stderr.write(
                "warning: --drift-budget given but no records replayed; "
                "treating as PASS\n"
            )
            sys.exit(0)
        if report.interp_drift_avg > drift_budget:
            sys.stderr.write(
                f"FAIL: interp_drift_avg={report.interp_drift_avg:.3f} "
                f"exceeds budget {drift_budget:.3f}\n"
            )
            sys.exit(5)
        if output_format == "text":
            print(
                f"  PASS: interp_drift_avg={report.interp_drift_avg:.3f} "
                f"<= budget {drift_budget:.3f}"
            )


def _run_analyze():
    config.ensure_home()
    from .cli import analyze
    analyze()


def _run_reindex():
    config.ensure_home()
    n = index.rebuild()
    print(f"reindexed {n} records into {config.SESSIONS_DB}")


def _run_logo(args):
    """`janus --logo` — print the brand mark.

    Three modes (mutually exclusive, --svg wins over --plain):
      default    ASCII logo + side titles + tagline + version
      --plain    bare 3-line ASCII logo only (for shell prompt embedding)
      --svg      vector SVG markup with literal brand color
    """
    from . import branding
    if "--svg" in args:
        sys.stdout.write(branding.svg_logo(branding.BRAND_COLOR) + "\n")
        return
    if "--plain" in args:
        for line in branding.LOGO_LINES:
            sys.stdout.write(line + "\n")
        return
    # Default: logo + side titles. Status block intentionally omitted —
    # this output is for piping into a shell prompt or alias.
    b = branding.BannerInputs(
        model="", cwd="", home="",
        tool_count=0, skill_count=0, mcp_count=0,
    )
    for logo, title in branding.logo_with_titles(b):
        sys.stdout.write(f"{logo}{title}\n")


_MEMORY_HELP = """\
janus memory subcommands (v1.18):
  janus memory stats             aggregate counts + recall stats
  janus memory search <query>    FTS5 search across cards + legacy .md
  janus memory show <id>         dump one card to stdout
  janus memory reindex           drop and rebuild SQLite cache from cards/
  janus memory pause             stop auto-extraction
  janus memory resume            resume auto-extraction
  janus memory prune             pure-compute prune by age + durability
  janus memory consolidate       LLM-driven reflection pass
  janus memory clear --type=<t>  destructive: move all cards of one type
                                 to _superseded/
"""


def _run_memory_cli(args: list[str]) -> None:
    """Dispatch `janus memory <subcmd>`."""
    config.ensure_home()
    if not args or args[0] in ("--help", "-h", "help"):
        print(_MEMORY_HELP); return
    sub = args[0]
    rest = args[1:]
    if sub == "stats":
        from . import memory_index
        try:
            memory_index.reconcile()
        except Exception:
            pass
        s = memory_index.summary()
        print(f"Memory cards: {s['total']}")
        if s["per_type"]:
            print("  by type:")
            for t, n in sorted(s["per_type"].items()):
                print(f"    {t}: {n}")
        if s["per_scope"]:
            print("  by scope:")
            for sc, n in sorted(s["per_scope"].items()):
                print(f"    {sc}: {n}")
        print(f"  total recalls: {s['total_recalls']}")
        if s["most_recalled"]:
            print("  most-recalled cards:")
            for r in s["most_recalled"]:
                print(f"    [{r['type']}:{r['subject']}] × {r['recall_count']}")
        paused = (config.MEMORY_DIR / "_paused").exists()
        print(f"  extraction: {'PAUSED' if paused else 'enabled'}")
        return
    if sub == "search":
        if not rest:
            print("usage: janus memory search <query>"); sys.exit(2)
        query = " ".join(rest)
        from . import memory_recall, memory_state
        cards = memory_recall.top_k(query, top_k=10, budget_bytes=2000)
        if cards:
            print(f"Cards ({len(cards)}):")
            for c in cards:
                print(f"  {c['_line']}  (id={c['id']} scope={c['scope']})")
        hits = memory_state.search_memory(query, top_k=10)
        if hits:
            print(f"\nLegacy .md matches ({len(hits)}):")
            for h in hits:
                print(f"  {h['category']}.md ## {h['section']}")
                print(f"    {h['line']}")
        if not cards and not hits:
            print(f"(no matches for {query!r})")
        return
    if sub == "show":
        if not rest:
            print("usage: janus memory show <card-id>"); sys.exit(2)
        from . import memory_cards
        p = memory_cards.card_path(rest[0])
        if not p.exists():
            print(f"card not found: {rest[0]}"); sys.exit(1)
        print(p.read_text("utf-8")); return
    if sub == "reindex":
        from . import memory_index
        memory_index.reset()
        counts = memory_index.reconcile()
        print(
            f"reindexed: {counts['added']} added, "
            f"{counts['updated']} updated, {counts['deleted']} dropped."
        )
        return
    if sub == "pause":
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (config.MEMORY_DIR / "_paused").touch()
        print("memory extraction paused"); return
    if sub == "resume":
        marker = config.MEMORY_DIR / "_paused"
        if marker.exists():
            marker.unlink()
        print("memory extraction enabled"); return
    if sub == "prune":
        from . import memory_prune
        counts = memory_prune.run_once()
        print(
            f"pruned: {counts['removed']} dropped "
            f"(active={counts['active_drops']}, "
            f"low_conf={counts['low_conf_drops']}, "
            f"superseded={counts['superseded_drops']})"
        )
        return
    if sub == "consolidate":
        try:
            from . import memory_consolidate
        except ImportError:
            print("consolidate module not available"); sys.exit(1)
        try:
            summary = memory_consolidate.run_once()
        except Exception as e:
            print(f"error: {type(e).__name__}: {e}"); sys.exit(1)
        print(
            f"consolidated: {summary['written']} reflection card(s) "
            f"from {summary['examined']} examined"
        )
        return
    if sub == "clear":
        from . import memory_cards, memory_index
        type_filter = None
        for token in rest:
            if token.startswith("--type="):
                type_filter = token[len("--type="):]
        if not type_filter or type_filter not in memory_cards.TYPES:
            print(
                "usage: janus memory clear --type=<one of "
                f"{', '.join(memory_cards.TYPES)}>"
            )
            sys.exit(2)
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
        print(f"moved {n} {type_filter} card(s) to _superseded/")
        return
    print(f"unknown memory subcommand: {sub}")
    print(_MEMORY_HELP)
    sys.exit(2)


def _run_swarm_cli(args: list[str]) -> None:
    """Dispatch `janus swarm <subcmd>`."""
    config.assert_configured()
    config.ensure_home()
    if not args or args[0] in ("--help", "-h", "help"):
        print(_SWARM_HELP); return
    sub = args[0]
    rest = args[1:]
    if sub == "list":
        _swarm_list(); return
    if sub == "describe":
        if not rest:
            print("usage: janus swarm describe <spec-name>"); sys.exit(2)
        _swarm_describe(rest[0]); return
    if sub == "run":
        if not rest:
            print("usage: janus swarm run <spec-name> [--background] [key=value ...]"); sys.exit(2)
        if "--background" in rest:
            cleaned = [r for r in rest if r != "--background"]
            _swarm_run_background(cleaned[0], cleaned[1:]); return
        _swarm_run(rest[0], rest[1:]); return
    if sub == "_bg_run":
        # Internal — invoked by _swarm_run_background. NOT in help.
        if len(rest) < 3:
            print("internal: _bg_run <run_id> <spec_name> <inputs_json>")
            sys.exit(2)
        _swarm_bg_child(rest[0], rest[1], rest[2]); return
    if sub == "status":
        if not rest:
            print("usage: janus swarm status <run-id>"); sys.exit(2)
        _swarm_status(rest[0]); return
    if sub == "cancel":
        if not rest:
            print("usage: janus swarm cancel <run-id>"); sys.exit(2)
        _swarm_cancel(rest[0]); return
    if sub == "cost":
        if not rest:
            print("usage: janus swarm cost <run-id>"); sys.exit(2)
        _swarm_cost(rest[0]); return
    if sub == "trace":
        if not rest:
            print("usage: janus swarm trace <run-id>"); sys.exit(2)
        _swarm_trace(rest[0]); return
    print(f"unknown swarm subcommand: {sub}\n\n{_SWARM_HELP}"); sys.exit(2)


_SWARM_HELP = """\
janus swarm — parallel sub-agent coordination driven by markdown specs.

  list                          list installed specs + recent runs
  describe <spec-name>          show a spec's frontmatter and phases
  run <spec-name> [--background] [key=value]  launch (--background detaches the run)
  status <run-id>               show current run state (last timeline event)
  cancel <run-id>               write cancel.flag (cooperative cancellation)
  cost <run-id>                 per-swarm cost breakdown by role + phase
  trace <run-id>                replay timeline.jsonl

Specs live under ~/.janus/swarms/specs/<name>.md (markdown + YAML frontmatter,
type: swarm). Runs at ~/.janus/swarms/runs/<run-id>/ — fully plain-text and
readable without jq."""


def _swarm_list() -> None:
    from . import swarms
    specs = swarms.spec.list_specs()
    runs = swarms.state.list_runs()
    print("specs:")
    if not specs:
        print("  (none — drop a markdown file under ~/.janus/swarms/specs/)")
    else:
        for s in specs:
            desc = (s.description or "").splitlines()[0][:60] if s.description else ""
            print(f"  {s.name:<30} v{s.version}  {desc}")
    print()
    print("recent runs (newest first):")
    if not runs:
        print("  (none)")
    else:
        for rid in runs[:10]:
            meta = swarms.state.read_metadata(rid) or {}
            spec_name = meta.get("spec_name", "?")
            print(f"  {rid}   spec={spec_name}")


def _swarm_describe(name: str) -> None:
    from . import swarms
    s = swarms.spec.find_spec(name)
    if s is None:
        print(f"no spec named {name!r}"); sys.exit(2)
    print(f"name:        {s.name}")
    print(f"version:     {s.version}")
    print(f"description: {s.description}")
    print(f"output:      {s.output_format}")
    print()
    print("budget:")
    print(f"  max_usd:                          ${s.budget.max_usd}")
    print(f"  max_wallclock_s:                   {s.budget.max_wallclock_s}")
    print(f"  max_subagents:                     {s.budget.max_subagents}")
    print(f"  max_recursion_depth:               {s.budget.max_recursion_depth}")
    print(f"  max_total_tool_calls:              {s.budget.max_total_tool_calls}")
    print(f"  max_completion_tokens_per_role:    {s.budget.max_completion_tokens_per_role}")
    print()
    print("inputs:")
    if not s.inputs:
        print("  (none)")
    for i in s.inputs:
        bits = [i.type]
        if i.required:
            bits.append("required")
        if i.default is not None:
            bits.append(f"default={i.default!r}")
        if i.min is not None:
            bits.append(f"min={i.min}")
        if i.max is not None:
            bits.append(f"max={i.max}")
        print(f"  {i.name:<20}  {' · '.join(bits)}")
    print()
    print(f"permissions: default_mode={s.permissions.default_mode}")
    if s.permissions.per_role:
        for role, mode in s.permissions.per_role.items():
            print(f"  {role:<20}  → {mode}")
    print()
    print(f"phases ({len(s.phases)}):")
    for i, p in enumerate(s.phases):
        dep = f" depends_on={p.depends_on}" if p.depends_on else ""
        model = f" model={p.model}" if p.model else " model=(default)"
        print(f"  [{i}] {p.name:<20} role={p.role}  {p.pattern}  → {p.aggregator}{model}{dep}")


def _parse_kv_args(args: list[str]) -> dict:
    """Parse k=v pairs; values are JSON-decoded if possible (so '5' → int,
    '"hi"' → string, '[1,2]' → list), else taken literal."""
    import json as _json
    out: dict = {}
    for a in args:
        if "=" not in a:
            print(f"error: argument {a!r} not in key=value form"); sys.exit(2)
        k, v = a.split("=", 1)
        try:
            out[k] = _json.loads(v)
        except Exception:
            out[k] = v
    return out


def _swarm_run(name: str, kv_args: list[str]) -> None:
    from . import swarms
    s = swarms.spec.find_spec(name)
    if s is None:
        print(f"no spec named {name!r}"); sys.exit(2)
    inputs = _parse_kv_args(kv_args)
    try:
        result = swarms.runner.run_swarm(s, inputs=inputs)
    except swarms.spec.SpecError as e:
        print(f"spec error: {e}"); sys.exit(2)
    print(f"run_id: {result.run_id}")
    if result.error:
        print(f"error: {result.error}")
        sys.exit(1)
    print(f"phases: {len(result.phases)}")
    for p in result.phases:
        n_err = sum(1 for s_ in p.sub_agents if s_.error)
        print(f"  {p.name}  sub-agents={len(p.sub_agents)}  errors={n_err}")
    print(f"final.json: ~/.janus/swarms/runs/{result.run_id}/final.json")


def _swarm_run_background(name: str, kv_args: list[str]) -> None:
    """Spawn a swarm in a detached child process, return run_id immediately.

    The parent pre-mints the run_id and writes the metadata stub so
    `janus swarm status` works as soon as the spawn returns. The child
    runs the swarm; its stdout/stderr go to {run_dir}/stdout.log and
    {run_dir}/stderr.log so logs persist after the parent exits.

    Detachment uses POSIX start_new_session=True or Windows DETACHED_PROCESS
    so the child survives parent exit (terminal close, ssh disconnect)."""
    import os
    import subprocess
    from . import swarms as _swarms

    spec = _swarms.spec.find_spec(name)
    if spec is None:
        print(f"no spec named {name!r}"); sys.exit(2)
    inputs = _parse_kv_args(kv_args)
    try:
        _swarms.spec.validate_inputs(spec, inputs)
    except _swarms.spec.SpecError as e:
        print(f"spec error: {e}"); sys.exit(2)

    run_id = _swarms.state.new_run_id()
    _swarms.state.init_run_dir(run_id)
    _swarms.state.write_metadata(run_id, {
        "started": _swarms.state._now_iso(),
        "spec_name": spec.name,
        "spec_version": spec.version,
        "background": True,
        "default_mode": spec.permissions.default_mode,
    })
    _swarms.state.append_timeline(run_id, {
        "type": "background_spawn",
        "spec": spec.name, "n_phases": len(spec.phases),
    })

    run_dir = _swarms.state.run_dir(run_id)
    out_log = open(run_dir / "stdout.log", "w", encoding="utf-8")
    err_log = open(run_dir / "stderr.log", "w", encoding="utf-8")

    child_cmd = [
        sys.executable, "-m", "janus", "swarm", "_bg_run",
        run_id, name, _json.dumps(inputs),
    ]
    spawn_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": out_log,
        "stderr": err_log,
        "close_fds": True,
    }
    if os.name == "posix":
        spawn_kwargs["start_new_session"] = True
    elif os.name == "nt":
        # Detach so child survives parent exit; DETACHED_PROCESS removes
        # the inherited console.
        spawn_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )

    try:
        proc = subprocess.Popen(child_cmd, **spawn_kwargs)
    except Exception as e:
        print(f"failed to spawn child: {type(e).__name__}: {e}")
        sys.exit(1)

    # Write the child PID to a file so cancel/status can find it.
    (run_dir / "pid").write_text(str(proc.pid), encoding="utf-8")

    print(f"swarm spawned in background")
    print(f"  run_id: {run_id}")
    print(f"  pid:    {proc.pid}")
    print(f"  status: janus swarm status {run_id}")
    print(f"  cancel: janus swarm cancel {run_id}")
    print(f"  logs:   {run_dir}/stdout.log + stderr.log")


def _swarm_bg_child(run_id: str, spec_name: str, inputs_json: str) -> None:
    """Child entry point invoked by _swarm_run_background's subprocess."""
    from . import swarms as _swarms
    try:
        inputs = _json.loads(inputs_json)
    except Exception as e:
        sys.stderr.write(f"bad inputs JSON: {e}\n")
        sys.exit(2)
    spec = _swarms.spec.find_spec(spec_name)
    if spec is None:
        sys.stderr.write(f"no spec named {spec_name!r}\n")
        sys.exit(2)
    try:
        result = _swarms.runner.run_swarm(
            spec, inputs=inputs, run_id_override=run_id,
        )
    except Exception as e:
        sys.stderr.write(f"swarm crashed: {type(e).__name__}: {e}\n")
        # Write final.json with the crash so status reads cleanly.
        _swarms.state.write_final(run_id, {
            "error": f"crashed: {type(e).__name__}: {e}",
        })
        # Best-effort completion ping even on crash.
        _ping_home_channel(run_id, spec_name, error=f"crashed: {e}")
        sys.exit(1)
    sys.stderr.write(f"swarm complete: {result.run_id}\n")
    _ping_home_channel(
        run_id, spec_name,
        error=result.error,
        n_phases=len(result.phases),
    )
    sys.exit(0 if not result.error else 1)


def _ping_home_channel(
    run_id: str, spec_name: str, *,
    error: str | None = None, n_phases: int | None = None,
) -> None:
    """Send a Telegram completion notification to the configured home
    channel. v1.5 phase 8.

    Best-effort: never raises. Silently no-ops if there's no Telegram
    bot token, no home channel configured, or the HTTP call fails.
    Composes with v1.3's home-channel system (gateways._common).
    """
    try:
        token = config.TELEGRAM_BOT_TOKEN
        if not token:
            return
        from .gateways import _common as gw
        home_chat = gw.get_home("telegram")
        if not home_chat:
            return
        if error:
            text = (
                f"🐝 swarm `{spec_name}` FAILED\n"
                f"run_id: `{run_id}`\n"
                f"error: {error}"
            )
        else:
            text = (
                f"🐝✓ swarm `{spec_name}` complete\n"
                f"run_id: `{run_id}`\n"
                f"phases: {n_phases if n_phases is not None else '?'}"
            )
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": home_chat, "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception:
        # P8: notification failure never propagates.
        pass


def _swarm_status(run_id: str) -> None:
    from . import swarms
    meta = swarms.state.read_metadata(run_id)
    if meta is None:
        print(f"no such run: {run_id}"); sys.exit(2)
    print(f"run_id: {run_id}")
    print(f"  spec:     {meta.get('spec_name')}")
    print(f"  started:  {meta.get('started')}")
    cancelled = swarms.state.is_cancelled(run_id)
    timeline = swarms.state.read_timeline(run_id)
    final = swarms.state.read_final(run_id)
    last_event = timeline[-1] if timeline else None
    if final is not None:
        if isinstance(final, dict) and final.get("error"):
            print(f"  status:   FAILED ({final['error']})")
        else:
            print(f"  status:   COMPLETE")
    elif cancelled:
        print(f"  status:   CANCELLED (running, will exit at next step)")
    else:
        print(f"  status:   RUNNING")
    if last_event:
        print(f"  last:     {last_event.get('type')} @ {last_event.get('ts')}")


def _swarm_cancel(run_id: str) -> None:
    from . import swarms
    if swarms.state.read_metadata(run_id) is None:
        print(f"no such run: {run_id}"); sys.exit(2)
    swarms.state.write_cancel_flag(run_id)
    print(f"cancellation flag written for {run_id}")
    print("(currently-running sub-agents will exit between steps)")


def _swarm_cost(run_id: str) -> None:
    from . import cost as cost_mod, swarms
    if swarms.state.read_metadata(run_id) is None:
        print(f"no such run: {run_id}"); sys.exit(2)
    print(cost_mod.render_per_swarm(run_id))


def _swarm_trace(run_id: str) -> None:
    from . import swarms
    timeline = swarms.state.read_timeline(run_id)
    if not timeline:
        print(f"no timeline for {run_id}"); sys.exit(2)
    for e in timeline:
        ts = e.get("ts", "")
        ev = e.get("type", "")
        rest = {k: v for k, v in e.items() if k not in ("ts", "type")}
        print(f"{ts}  {ev:<20}  {rest}")


def main():
    args = sys.argv[1:]
    # Phase 16: headless detection runs first — `-p`, `--prompt`, or stdin pipe.
    if _is_headless_invocation(args):
        _run_headless(args); return
    if not args:
        _run_chat(); return
    sub = args[0]
    if sub in ("--analyze", "-a"):
        _run_analyze(); return
    if sub == "--reindex":
        _run_reindex(); return
    if sub == "--eval":
        _run_eval(); return
    if sub in ("--version", "-V"):
        from . import branding
        print(f"janus {branding.VERSION}")
        return
    if sub == "--logo":
        _run_logo(args); return
    if sub == "--conversations":
        _list_conversations(); return
    if sub in ("--continue", "--resume"):
        _run_chat(); return
    if sub in ("chat", "--chat"):
        _run_chat(); return
    if sub == "telegram":
        _run_telegram(); return
    if sub == "tui":
        from . import tui as _tui
        sys.exit(_tui.serve())
    if sub == "web":
        _run_web(args); return
    if sub == "whatsapp":
        _run_whatsapp(args); return
    if sub == "daemon":
        once = "--once" in args
        _run_daemon(once=once); return
    if sub == "fire":
        if len(args) < 2:
            print("usage: python -m janus fire <trigger-name>"); sys.exit(2)
        _fire(args[1]); return
    if sub == "pair":
        _run_pair(args[1:]); return
    if sub == "uninstall":
        _run_uninstall(args[1:]); return
    if sub == "swarm":
        _run_swarm_cli(args[1:]); return
    if sub == "insights":
        _run_insights(args[1:]); return
    if sub == "stats":
        from . import rate_limit
        print(rate_limit.render_summary(rate_limit.get_summary())); return
    if sub == "memory":
        _run_memory_cli(args[1:]); return
    if sub == "onboard":
        from . import onboarding
        ok = onboarding.run_wizard()
        sys.exit(0 if ok else 1)
    # v1.33.1 — backup / restore for production state preservation.
    if sub == "backup":
        from . import backup as _bk
        sys.exit(_bk.cmd_backup(args[1:]))
    if sub == "restore":
        from . import backup as _bk
        sys.exit(_bk.cmd_restore(args[1:]))
    # v1.33.5 — audit log filter (Phase 6.6).
    if sub == "audit":
        from . import audit_log
        sys.exit(audit_log.cmd_audit(args[1:]))
    if sub == "service":
        from . import services
        sub_args = args[1:] if len(args) > 1 else ["status"]
        action = sub_args[0] if sub_args else "status"
        if action == "install":
            sys.exit(services.cmd_install(force="--force" in sub_args[1:]))
        elif action == "enable":
            sys.exit(services.cmd_enable())
        elif action in ("disable", "stop"):
            sys.exit(services.cmd_disable())
        elif action == "status":
            sys.exit(services.cmd_status())
        elif action == "uninstall":
            sys.exit(services.cmd_uninstall())
        elif action == "show" and len(sub_args) > 1:
            sys.exit(services.cmd_show(sub_args[1]))
        else:
            print(
                "usage: janus service {install [--force] | enable | "
                "disable | status | uninstall | show <name>}"
            )
            sys.exit(2)
    if sub in ("--help", "-h", "help"):
        print(__doc__); return
    _run_chat()


def _run_insights(args: list[str]) -> None:
    """`janus insights [--days N]` — print the insights report.

    No API call required; deterministic stats from local files. Plays
    well in cron / pipelines (`janus insights | mail -s "weekly"`).
    """
    from . import insights as _ins
    days = 7
    if "--days" in args:
        try:
            i = args.index("--days")
            days = int(args[i + 1])
        except (IndexError, ValueError):
            print("usage: janus insights [--days N]")
            sys.exit(2)
    days = max(1, min(days, 365))
    stats = _ins.compute_insights(days=days)
    print(_ins.render_insights(stats))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
