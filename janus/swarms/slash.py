"""
swarms/slash.py — single handler shared by every user-facing surface
that needs `/swarm` slash commands (cli_rich, cli, telegram, web,
whatsapp). Each surface calls handle(arg_string) and prints the result.

Returns plain text (no ANSI / no markdown formatting) so it renders
correctly across every surface — gateways can wrap it however they like.

Subcommands:
  /swarm                       help
  /swarm list                  installed specs + recent runs
  /swarm describe <name>       one spec's details
  /swarm run <name> [k=v...]   launch a swarm; inputs as key=value
  /swarm status <run-id>       show current run state
  /swarm cancel <run-id>       cooperative cancellation
"""

from __future__ import annotations

from .. import cost as cost_mod
from . import runner, spec, state


_HELP = """\
/swarm — parallel sub-agent coordination
  list                       installed specs + recent runs
  describe <spec-name>       one spec's details
  run <spec-name> [k=v ...]  launch a swarm
  status <run-id>            current run state
  cancel <run-id>            cooperative cancellation"""


def handle(arg: str) -> str:
    """Parse /swarm <arg> and return text to display."""
    parts = (arg or "").strip().split(maxsplit=1)
    if not parts:
        return _HELP
    sub = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if sub in ("help", "--help", "-h"):
        return _HELP
    if sub == "list":
        return _list()
    if sub == "describe":
        return _describe(rest.strip())
    if sub == "run":
        return _run(rest)
    if sub == "status":
        return _status(rest.strip())
    if sub == "cancel":
        return _cancel(rest.strip())
    return f"unknown /swarm subcommand: {sub}\n\n{_HELP}"


# ---------- Subcommand handlers ----------


def _list() -> str:
    specs = spec.list_specs()
    runs = state.list_runs()
    lines = ["specs:"]
    if not specs:
        lines.append("  (none — drop a markdown file under ~/.janus/swarms/specs/)")
    else:
        for s in specs:
            desc = (s.description or "").splitlines()[0][:60]
            lines.append(f"  {s.name:<30} v{s.version}  {desc}")
    lines.append("")
    lines.append("recent runs (newest first):")
    if not runs:
        lines.append("  (none)")
    else:
        for rid in runs[:10]:
            meta = state.read_metadata(rid) or {}
            spec_name = meta.get("spec_name", "?")
            lines.append(f"  {rid}   spec={spec_name}")
    return "\n".join(lines)


def _describe(name: str) -> str:
    if not name:
        return "usage: /swarm describe <spec-name>"
    s = spec.find_spec(name)
    if s is None:
        return f"no spec named {name!r}"
    lines = [
        f"name:        {s.name}",
        f"version:     {s.version}",
        f"description: {s.description}",
        f"output:      {s.output_format}",
        "",
        "budget:",
        f"  max_usd:                 ${s.budget.max_usd}",
        f"  max_wallclock_s:          {s.budget.max_wallclock_s}",
        f"  max_subagents:            {s.budget.max_subagents}",
        f"  max_total_tool_calls:     {s.budget.max_total_tool_calls}",
        "",
    ]
    if s.inputs:
        lines.append("inputs:")
        for i in s.inputs:
            bits = [i.type]
            if i.required:
                bits.append("required")
            if i.default is not None:
                bits.append(f"default={i.default!r}")
            lines.append(f"  {i.name:<15}  {' · '.join(bits)}")
        lines.append("")
    lines.append(f"phases ({len(s.phases)}):")
    for i, p in enumerate(s.phases):
        dep = f" depends_on={p.depends_on}" if p.depends_on else ""
        model = f" model={p.model}" if p.model else ""
        lines.append(
            f"  [{i}] {p.name:<15} role={p.role}  {p.pattern}  → {p.aggregator}{model}{dep}"
        )
    return "\n".join(lines)


def _run(arg: str) -> str:
    """`<name> [k=v ...]` — parses spec name + key=value inputs."""
    parts = arg.strip().split()
    if not parts:
        return "usage: /swarm run <spec-name> [key=value ...]"
    name = parts[0]
    s = spec.find_spec(name)
    if s is None:
        return f"no spec named {name!r}"
    try:
        inputs = _parse_kv(parts[1:])
    except ValueError as e:
        return f"input error: {e}"
    try:
        result = runner.run_swarm(s, inputs=inputs)
    except spec.SpecError as e:
        return f"spec error: {e}"
    if result.error:
        return f"run_id: {result.run_id}\nerror: {result.error}"
    return (
        f"run_id: {result.run_id}\n"
        f"phases: {len(result.phases)}\n"
        + "\n".join(
            f"  {p.name}  sub-agents={len(p.sub_agents)}"
            f"  errors={sum(1 for s in p.sub_agents if s.error)}"
            for p in result.phases
        )
        + f"\nfinal: ~/.janus/swarms/runs/{result.run_id}/final.json"
    )


def _status(run_id: str) -> str:
    if not run_id:
        return "usage: /swarm status <run-id>"
    meta = state.read_metadata(run_id)
    if meta is None:
        return f"no such run: {run_id}"
    cancelled = state.is_cancelled(run_id)
    final = state.read_final(run_id)
    timeline = state.read_timeline(run_id)
    last = timeline[-1] if timeline else None
    if final is not None:
        if isinstance(final, dict) and final.get("error"):
            status = f"FAILED ({final['error']})"
        else:
            status = "COMPLETE"
    elif cancelled:
        status = "CANCELLED (will exit at next step)"
    else:
        status = "RUNNING"
    lines = [
        f"run_id:  {run_id}",
        f"  spec:    {meta.get('spec_name')}",
        f"  started: {meta.get('started')}",
        f"  status:  {status}",
    ]
    if last:
        lines.append(f"  last:    {last.get('type')} @ {last.get('ts')}")
    return "\n".join(lines)


def _cancel(run_id: str) -> str:
    if not run_id:
        return "usage: /swarm cancel <run-id>"
    if state.read_metadata(run_id) is None:
        return f"no such run: {run_id}"
    state.write_cancel_flag(run_id)
    return (
        f"cancellation flag written for {run_id}\n"
        "(currently-running sub-agents will exit between steps)"
    )


def _parse_kv(args: list[str]) -> dict:
    import json as _json
    out: dict = {}
    for a in args:
        if "=" not in a:
            raise ValueError(f"argument {a!r} not in key=value form")
        k, v = a.split("=", 1)
        try:
            out[k] = _json.loads(v)
        except Exception:
            out[k] = v
    return out
