"""
swarms/state.py — ~/.janus/swarms/runs/<run-id>/ layout helpers.

Layout per the v1.4 plan:
  ~/.janus/swarms/runs/<run-id>/
    spec.md                                # frozen copy of spec used
    inputs.json                            # validated launch inputs
    metadata.json                          # {started, parent_run_id, models, ...}
    cost.jsonl                             # per-sub-agent cost rows (phase 4)
    timeline.jsonl                         # phase events (single writer = parent)
    cancel.flag                            # presence = cancellation requested (phase 7)
    phase_<NN>_<name>/
      input.json                           # what this phase received
      aggregated.json                      # what the aggregator produced
      agents/<agent_id>.jsonl              # per-sub-agent trace (one file per agent)
    final.json                             # final aggregated output

run-id  = swarm-YYYY-MM-DDTHH-MM-SS-<hex4>   (sortable, UTC)
agent_id = "{role}-{idx:03d}-{hex4}"          (e.g., scraper-007-a3f2)

Atomic writes via tempfile + os.replace (same pattern as memory.py).
One file per sub-agent so concurrent appends don't interleave bytes
(POSIX append-atomicity is only ~PIPE_BUF; Windows is worse). Parent
thread is the sole writer of timeline.jsonl and cost.jsonl.
"""

from __future__ import annotations
import datetime as _dt
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from .. import config


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_run_id() -> str:
    """Sortable, unique: swarm-2026-05-03T14-30-45-abc1"""
    now = _dt.datetime.now(_dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H-%M-%S")
    return f"swarm-{ts}-{secrets.token_hex(2)}"


def new_agent_id(role: str, idx: int) -> str:
    """Stable per-sub-agent id: scraper-007-a3f2.
    Hex suffix prevents collisions across runs that happen to use the
    same role/idx pair."""
    return f"{role}-{idx:03d}-{secrets.token_hex(2)}"


# ---------- Path helpers ----------


def runs_root() -> Path:
    """The runs directory, queried at call time so tests that monkeypatch
    config.SWARM_RUNS_DIR see the new value."""
    return config.SWARM_RUNS_DIR


def run_dir(run_id: str) -> Path:
    return runs_root() / run_id


def phase_dir(run_id: str, phase_num: int, phase_name: str) -> Path:
    return run_dir(run_id) / f"phase_{phase_num:02d}_{phase_name}"


def agents_dir(run_id: str, phase_num: int, phase_name: str) -> Path:
    return phase_dir(run_id, phase_num, phase_name) / "agents"


# ---------- Atomic writes ----------


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically via tmpfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON record. Single-writer assumption per file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ---------- Run lifecycle ----------


def init_run_dir(run_id: str) -> Path:
    """Create the run directory. Returns its absolute path."""
    d = run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def init_phase_dir(run_id: str, phase_num: int, phase_name: str) -> Path:
    d = phase_dir(run_id, phase_num, phase_name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "agents").mkdir(parents=True, exist_ok=True)
    return d


def freeze_spec(run_id: str, spec_text: str) -> None:
    """Copy the spec verbatim into the run dir. The spec is the contract;
    freezing it lets you replay/audit the exact spec used even if the
    file at ~/.janus/swarms/specs/<name>.md is later edited."""
    p = run_dir(run_id) / "spec.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(spec_text, encoding="utf-8")


def write_inputs(run_id: str, inputs: dict) -> None:
    atomic_write_json(run_dir(run_id) / "inputs.json", inputs)


def write_metadata(run_id: str, metadata: dict) -> None:
    atomic_write_json(run_dir(run_id) / "metadata.json", metadata)


def write_phase_input(
    run_id: str, phase_num: int, phase_name: str, data: Any,
) -> None:
    atomic_write_json(
        phase_dir(run_id, phase_num, phase_name) / "input.json", data,
    )


def write_phase_aggregated(
    run_id: str, phase_num: int, phase_name: str, data: Any,
) -> None:
    atomic_write_json(
        phase_dir(run_id, phase_num, phase_name) / "aggregated.json", data,
    )


def read_phase_aggregated(
    run_id: str, phase_num: int, phase_name: str,
) -> Any:
    p = phase_dir(run_id, phase_num, phase_name) / "aggregated.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_final(run_id: str, data: Any) -> None:
    atomic_write_json(run_dir(run_id) / "final.json", data)


def write_agent_transcript(
    run_id: str,
    phase_num: int,
    phase_name: str,
    agent_id: str,
    trace: list[dict],
) -> None:
    """Write one sub-agent's trace as JSONL. Single writer per file =
    no interleave hazard."""
    p = agents_dir(run_id, phase_num, phase_name) / f"{agent_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for record in trace:
            f.write(json.dumps(record, default=str) + "\n")


def append_timeline(run_id: str, event: dict) -> None:
    """Append an event to timeline.jsonl. ts auto-attached.
    Single-writer (the coordinator thread) — no lock needed."""
    record = {**event, "ts": _now_iso()}
    append_jsonl(run_dir(run_id) / "timeline.jsonl", record)


# ---------- Cancellation (phase 7 wires the polling loop) ----------


def write_cancel_flag(run_id: str) -> None:
    p = run_dir(run_id) / "cancel.flag"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def is_cancelled(run_id: str) -> bool:
    return (run_dir(run_id) / "cancel.flag").exists()


# ---------- Listing / inspection ----------


def list_runs() -> list[str]:
    """All swarm run-ids, newest-first by name (which is timestamp-prefixed)."""
    root = runs_root()
    if not root.is_dir():
        return []
    return sorted(
        (d.name for d in root.iterdir() if d.is_dir()),
        reverse=True,
    )


def read_metadata(run_id: str) -> dict | None:
    p = run_dir(run_id) / "metadata.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def read_final(run_id: str) -> Any:
    p = run_dir(run_id) / "final.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def read_timeline(run_id: str) -> list[dict]:
    p = run_dir(run_id) / "timeline.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
