"""
eval.py — replay log entries against current code to detect drift.

WHY:
"Self-improving" is wishful thinking without a regression signal. This module
replays prior log.jsonl entries against the current interpreter (and optionally
executor) and reports drift.

WHAT IT DOES NOT DO:
We don't claim ground truth. The user's previous choice was "what they wanted
that day" — drift isn't automatically bad. The harness FLAGS drift; humans
decide whether the new behavior is better.

CHEAP MODE (default):
  Replay interpreter only. ~1 LLM call per record.

FULL MODE (--with-execute):
  Also replay the executor against the previously-chosen interpretation. Many
  more LLM calls + tool runs. Use sparingly.
"""

from __future__ import annotations
import datetime
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, interpreter, logger, memory


# ---------- Result types ----------


@dataclass
class RecordEval:
    ts: str
    request: str
    original_choice: str
    original_labels: list[str]
    new_labels: list[str]
    interp_drift: int                # 0 identical, 1 partial, 2 disjoint
    interp_overlap: float            # |∩| / |∪|
    notes: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    started: str
    ended: str
    n_records: int
    interp_drift_avg: float
    interp_overlap_avg: float
    drift_distribution: dict[str, int]
    by_record: list[RecordEval]
    config: dict

    def to_json(self) -> dict:
        return {
            "started": self.started,
            "ended": self.ended,
            "n_records": self.n_records,
            "interp_drift_avg": self.interp_drift_avg,
            "interp_overlap_avg": self.interp_overlap_avg,
            "drift_distribution": self.drift_distribution,
            "config": self.config,
            "by_record": [
                {
                    "ts": r.ts,
                    "request": r.request,
                    "original_choice": r.original_choice,
                    "original_labels": r.original_labels,
                    "new_labels": r.new_labels,
                    "interp_drift": r.interp_drift,
                    "interp_overlap": r.interp_overlap,
                    "notes": r.notes,
                }
                for r in self.by_record
            ],
        }


# ---------- Drift metrics ----------


_WORD_RX = re.compile(r"[a-z0-9]+")


def _normalize_label(s: str) -> set[str]:
    return set(_WORD_RX.findall(s.lower()))


def label_jaccard(a: list[str], b: list[str]) -> float:
    sa = set()
    for x in a:
        sa |= _normalize_label(x)
    sb = set()
    for x in b:
        sb |= _normalize_label(x)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def classify_interp_drift(overlap: float) -> int:
    if overlap >= 0.85:
        return 0
    if overlap >= 0.30:
        return 1
    return 2


# ---------- Harness ----------


def replay(
    last_n: int | None = None,
    *,
    use_memory: bool = True,
    write_report: bool = True,
    skill_filter: str | None = None,
) -> RunReport:
    """Replay recent log records through the interpreter at temperature=0.

    `use_memory`: prepend current user.md (matches default runtime). Set
        False to compare interpretations independent of memory state.
    `skill_filter`: if set, only replay records that referenced this skill
        (i.e. record["skill"] == skill_filter). Phase 7: per-skill eval.
    """
    last_n = last_n or config.EVAL_DEFAULT_LAST
    records = [r for r in logger.read_all() if r.get("interpretations")]
    if skill_filter:
        records = [r for r in records if r.get("skill") == skill_filter]
    records = records[-last_n:]

    started = _now_iso()
    by_record: list[RecordEval] = []
    preamble = memory.prepend_for_prompt() if use_memory else ""

    for rec in records:
        req = rec.get("request", "")
        original_labels = [str(x.get("label", "")) for x in rec.get("interpretations") or []]
        try:
            new_interps = interpreter.interpret(
                req,
                memory_preamble=preamble,
                temperature=0.0,
            )
            new_labels = [x["label"] for x in new_interps]
            notes: list[str] = []
        except Exception as e:
            new_labels = []
            notes = [f"interpreter_error: {type(e).__name__}: {e}"]
        overlap = label_jaccard(original_labels, new_labels)
        drift = classify_interp_drift(overlap)
        by_record.append(RecordEval(
            ts=rec.get("ts", ""),
            request=req,
            original_choice=str(rec.get("choice", "")),
            original_labels=original_labels,
            new_labels=new_labels,
            interp_drift=drift,
            interp_overlap=overlap,
            notes=notes,
        ))

    ended = _now_iso()
    drift_dist = {"0": 0, "1": 0, "2": 0}
    for r in by_record:
        drift_dist[str(r.interp_drift)] += 1

    avg_drift = sum(r.interp_drift for r in by_record) / max(1, len(by_record))
    avg_overlap = sum(r.interp_overlap for r in by_record) / max(1, len(by_record))

    report = RunReport(
        started=started,
        ended=ended,
        n_records=len(by_record),
        interp_drift_avg=avg_drift,
        interp_overlap_avg=avg_overlap,
        drift_distribution=drift_dist,
        by_record=by_record,
        config={
            "model": config.MODEL,
            "use_memory": use_memory,
            "last_n": last_n,
            "skill_filter": skill_filter,
        },
    )
    if write_report:
        _write_report(report)
    return report


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _write_report(report: RunReport) -> Path:
    config.ensure_home()
    safe_ts = report.started.replace(":", "-")
    out = config.EVALS_DIR / f"run-{safe_ts}.json"
    out.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    return out


def render_summary(report: RunReport) -> str:
    """One-screen summary of a replay run."""
    lines = [
        f"  records replayed: {report.n_records}",
        f"  avg interp drift: {report.interp_drift_avg:.2f}   (0=identical, 2=disjoint)",
        f"  avg label overlap: {report.interp_overlap_avg:.2%}",
        f"  drift distribution:",
        f"    identical (0):  {report.drift_distribution['0']}",
        f"    partial   (1):  {report.drift_distribution['1']}",
        f"    disjoint  (2):  {report.drift_distribution['2']}",
        "",
    ]
    if report.n_records:
        lines.append("  worst-drift records:")
        worst = sorted(report.by_record, key=lambda r: -r.interp_drift)[:5]
        for r in worst:
            lines.append(f"    [{r.interp_drift}] {r.request[:60]!r}")
            lines.append(f"        was: {r.original_labels}")
            lines.append(f"        now: {r.new_labels}")
    return "\n".join(lines)
