"""
cost.py — token + dollar accounting (Phase 13).

WHY:
Hermes' adoption in enterprise is bottlenecked partly by silent bill
shock — agents that rack up $30 of API spend mid-loop with no warning.
Janus's local-first, manual-promotion thesis only holds if the user can
SEE the cost as it accumulates.

MODEL:
- Per-turn counter — reset by `new_turn()` at the start of each user
  request. Records `prompt_tokens`, `completion_tokens`, and `usd`.
- Per-session counter — accumulates from session boot.
- A small built-in price table covers common models (OpenAI, Anthropic,
  Google) at 2026-05 list prices. Override or extend via env var
  `JANUS_MODEL_PRICES_JSON` for new / private models.

INTEGRATION:
`llm.chat()` calls `cost.record(model, usage)` after every API call.
The CLI prints `cost.turn_summary()` on `/cost`.

NEVER RAISES (P8):
Pricing for an unknown model returns 0.0 USD with a "model not in price
table" hint. We don't crash on missing data.
"""

from __future__ import annotations
import datetime
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config


# ---------- Price table (USD per million tokens) ----------
# As of 2026-05. Override via JANUS_MODEL_PRICES_JSON for new models.
_BUILTIN_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "openai/gpt-4o":           (5.00, 15.00),
    "openai/gpt-4o-mini":      (0.15,  0.60),
    "openai/gpt-4.1":          (5.00, 15.00),
    "openai/gpt-4.1-mini":     (0.40,  1.60),
    # Anthropic
    "anthropic/claude-opus-4-7":      (15.00, 75.00),
    "anthropic/claude-sonnet-4-6":    (3.00,  15.00),
    "anthropic/claude-haiku-4-5":     (0.80,   4.00),
    # Google
    "google/gemini-2.5-pro":          (1.25, 10.00),
    "google/gemini-2.5-flash":        (0.30,  2.50),
    # Local — free.
    "local/llama":                    (0.0, 0.0),
}


def _price_table() -> dict[str, tuple[float, float]]:
    """Combine built-in + user override. User override wins on conflict."""
    out = dict(_BUILTIN_PRICES)
    raw = config.MODEL_PRICES_JSON or ""
    if raw.strip():
        try:
            extra = json.loads(raw)
            for model, spec in extra.items():
                if isinstance(spec, dict):
                    out[model] = (
                        float(spec.get("input_per_million") or 0),
                        float(spec.get("output_per_million") or 0),
                    )
                elif isinstance(spec, (list, tuple)) and len(spec) == 2:
                    out[model] = (float(spec[0]), float(spec[1]))
        except Exception:
            pass
    return out


# ---------- Counters ----------


@dataclass
class TokenStats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    usd: float = 0.0

    def add(self, prompt: int, completion: int, usd: float) -> None:
        self.prompt_tokens += int(prompt or 0)
        self.completion_tokens += int(completion or 0)
        self.usd += float(usd or 0.0)
        self.calls += 1

    def reset(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usd = 0.0
        self.calls = 0


_SESSION = TokenStats()
_TURN = TokenStats()
_BY_MODEL: dict[str, TokenStats] = {}


def session_stats() -> TokenStats:
    return _SESSION


def turn_stats() -> TokenStats:
    return _TURN


def by_model() -> dict[str, TokenStats]:
    return dict(_BY_MODEL)


def new_turn() -> None:
    """Reset the per-turn counters. Session counters keep accumulating.
    Called by cli.py at the start of each user request."""
    _TURN.reset()


def reset_session() -> None:
    """For `/clear` — both counters drop to zero."""
    _SESSION.reset()
    _TURN.reset()
    _BY_MODEL.clear()


# ---------- Recording ----------


def estimate_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    table = _price_table()
    rate = table.get(model)
    if rate is None:
        # Try a normalized lookup (some providers prefix with org/).
        for key, val in table.items():
            if key.endswith("/" + model.split("/")[-1]):
                rate = val
                break
    if rate is None:
        return 0.0
    inp, outp = rate
    return (prompt_tokens * inp / 1_000_000.0) + (completion_tokens * outp / 1_000_000.0)


def record(model: str, usage: dict | None) -> None:
    """Accumulate usage from one llm.chat() call. Safe to call with None
    or partial usage dicts — common when local providers don't report it."""
    if not usage:
        return
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    usd = estimate_usd(model, pt, ct)
    _TURN.add(pt, ct, usd)
    _SESSION.add(pt, ct, usd)
    bm = _BY_MODEL.setdefault(model, TokenStats())
    bm.add(pt, ct, usd)


# ---------- Display helpers ----------


def render_summary() -> str:
    """Pretty-print for `/cost`. ASCII-safe; both CLIs print directly."""
    lines = []
    lines.append("  this turn:")
    lines.append(
        f"    {_TURN.calls} calls · "
        f"{_TURN.prompt_tokens:,} in · "
        f"{_TURN.completion_tokens:,} out · "
        f"${_TURN.usd:.4f}"
    )
    lines.append("  this session:")
    lines.append(
        f"    {_SESSION.calls} calls · "
        f"{_SESSION.prompt_tokens:,} in · "
        f"{_SESSION.completion_tokens:,} out · "
        f"${_SESSION.usd:.4f}"
    )
    if _BY_MODEL:
        lines.append("  by model:")
        for model, st in sorted(_BY_MODEL.items(), key=lambda kv: -kv[1].usd):
            lines.append(
                f"    {model:<35}  "
                f"{st.prompt_tokens:>8,} in · "
                f"{st.completion_tokens:>8,} out · "
                f"${st.usd:.4f}"
            )
    if _SESSION.usd == 0.0 and _SESSION.prompt_tokens > 0:
        lines.append(
            "  (no $ shown: model not in price table — see "
            "JANUS_MODEL_PRICES_JSON to add)"
        )
    return "\n".join(lines)


# ---------- Per-chat ledger (v1.3 L3 #2) ----------
#
# In-process counters reset on restart. The per-chat ledger is JSONL on
# disk so one chat's spend is queryable across restarts and feeds
# cost-cartographer (the v1.2 skill that builds per-task cost models).


def _ledger_path() -> Path:
    return config.HOME / "cost.jsonl"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def record_per_chat(
    *,
    gateway: str,
    chat_id: str,
    identity: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    usd: float = 0.0,
) -> None:
    """Append one row to ~/.janus/cost.jsonl for this turn.

    Cheap and safe: open in append mode, JSON-encode one line, never
    raises (P8). Gateways call this AFTER executor.chat() returns —
    typically with cost.turn_stats() snapshotted from the just-run turn.
    """
    config.ensure_home()
    row = {
        "ts": _now_iso(),
        "gateway": gateway,
        "chat_id": str(chat_id),
        "identity": identity or "",
        "model": model or "",
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "usd": round(float(usd), 6),
    }
    try:
        with open(_ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def per_chat_summary(
    *,
    gateway: str = "",
    chat_id: str = "",
    identity: str = "",
    since_iso: str = "",
) -> TokenStats:
    """Sum the cost.jsonl ledger filtered by any combination of fields.

    Empty filters = sum everything. `since_iso` is an ISO-8601 cutoff
    (entries with `ts < since_iso` are excluded). Returns a fresh
    TokenStats — does NOT mutate the global counters.
    """
    out = TokenStats()
    p = _ledger_path()
    if not p.is_file():
        return out
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_iso and str(row.get("ts", "")) < since_iso:
                    continue
                if gateway and row.get("gateway") != gateway:
                    continue
                if chat_id and str(row.get("chat_id")) != str(chat_id):
                    continue
                if identity and row.get("identity") != identity:
                    continue
                out.add(
                    int(row.get("prompt_tokens") or 0),
                    int(row.get("completion_tokens") or 0),
                    float(row.get("usd") or 0.0),
                )
    except OSError:
        pass
    return out


def render_per_chat(gateway: str, chat_id: str, identity: str = "") -> str:
    """Pretty-print summary for one chat (used by gateway /cost)."""
    st = per_chat_summary(gateway=gateway, chat_id=chat_id)
    label = f"{gateway} chat={chat_id}"
    if identity:
        st_id = per_chat_summary(identity=identity)
        return (
            f"  this chat ({label}): "
            f"{st.calls} calls · ${st.usd:.4f}\n"
            f"  identity '{identity}' total: "
            f"{st_id.calls} calls · ${st_id.usd:.4f}"
        )
    return f"  this chat ({label}): {st.calls} calls · ${st.usd:.4f}"
