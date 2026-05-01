"""Entry point: python -m janus [subcommand] [flags].

  chat                          default -- interactive REPL (rich if available)
  telegram                      run the Telegram gateway (needs JANUS_TELEGRAM_TOKEN)
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

import sys

# Windows: console default is cp1252 which can't render the ╱─► / ┄ / ► /
# box-drawing chars in branding.py. Reconfigure to utf-8 once at entry so
# the banner doesn't crash. No-op on Linux/macOS where utf-8 is default.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

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


def _run_chat():
    _stash_conversation_from_flags(sys.argv[1:])
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
    if sub in ("--help", "-h", "help"):
        print(__doc__); return
    _run_chat()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
