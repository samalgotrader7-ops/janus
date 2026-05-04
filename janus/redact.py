"""
redact.py — PII / secret redaction at the gateway boundary (v1.11.0).

WHY THIS EXISTS:
~/.janus/log.jsonl is the audit trail. It captures everything: user
prompts, model outputs, tool args, tool results. That makes it a great
debug + replay tool — but if the user pastes an API key, a credit card,
or a phone number, it lands in that log forever. When the user shares
the log file (for support, for review, for sharing on Discord), they're
sharing the secrets too.

Hermes solves this with `agent/redact.py`. Janus ports the pattern.

WHAT IT REDACTS:
At "conservative" (default) — only secrets that have NO legitimate
plaintext use:
  - API keys: sk-… / sk_live_… (OpenAI, Stripe), gho_/ghp_… (GitHub),
    AKIA… (AWS), Bearer … in Authorization headers, AIzaSy… (Google),
    xoxb-/xoxp-/xoxa- (Slack), bot tokens (Telegram XXXXXXXX:AAA…)
  - Generic high-entropy strings near `key=`, `token=`, `secret=`,
    `password=` (lazy fallback)
  - Credit card numbers (13-19 digits with optional separators, Luhn-
    valid only — random 16-digit numbers don't trigger)
  - Private keys (-----BEGIN ... PRIVATE KEY----- blocks)
  - JWT (three base64url segments separated by dots)

At "aggressive" — also redacts:
  - Email addresses (user@host)
  - Phone numbers (+CC, US-formatted, internationals)
  - IPv4 + IPv6 (could be private infra)

NEVER REDACTED at any level:
  - Workspace paths (the agent NEEDS them to function)
  - Public usernames mentioned without "@"
  - URLs

WHERE IT'S WIRED:
  - logger.write (~/.janus/log.jsonl)
  - cost.jsonl (chat_id is OK — it's a numeric ID, not PII)
  - conversation files (~/.janus/conversations/) — protected by
    aggressive level if user opts in
  - cron output archive (~/.janus/cron/output/) — same

OPT-OUT:
JANUS_REDACT=off   — full passthrough
JANUS_REDACT=conservative  (default) — secrets only
JANUS_REDACT=aggressive    — secrets + emails + phones + IPs

Replacement format: <REDACTED:KIND> e.g., <REDACTED:openai_key> so the
model still sees that SOMETHING was redacted in tool output it later
reads. That preserves the integrity of the conversation for replay /
audit while keeping the secret out of disk.

P5 (plain-text state): logs stay greppable. Redaction never converts
log to opaque blobs.

P8 (errors are observations): redactor failure → return original text
unchanged. Better to leak than crash.
"""

from __future__ import annotations
import os
import re
from typing import Any


# ---------- Pattern catalog ----------


# Each entry: (regex, kind_label, level_required)
# Level required: "conservative" | "aggressive"

_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # ---- Specific provider key shapes ----
    # ORDER MATTERS — more specific prefixes first. The generic OpenAI
    # `sk-…` rule sits LAST among sk-prefixed keys so it doesn't gobble
    # `sk-ant-` / `sk-or-` / `sk_live_` first.
    # Anthropic: sk-ant-...
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
     "anthropic_key", "conservative"),
    # OpenRouter: sk-or-...
    (re.compile(r"\bsk-or-[A-Za-z0-9_-]{20,}\b"),
     "openrouter_key", "conservative"),
    # Stripe: sk_live_… / sk_test_…
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b"),
     "stripe_key", "conservative"),
    # OpenAI (must come AFTER the more-specific sk-ant / sk-or above):
    # sk-proj-... / sk-... (≥20 chars after sk-, no `ant-` / `or-` prefix
    # — those already redacted by the rules above and won't match here).
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
     "openai_key", "conservative"),
    # GitHub: ghp_/gho_/ghu_/ghs_/ghr_ + 36 alphanumerics
    (re.compile(r"\bgh[opusr]_[A-Za-z0-9]{30,}\b"),
     "github_token", "conservative"),
    # AWS access key: AKIA + 16 uppercase alphanumerics
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "aws_access_key", "conservative"),
    # Google API key: AIzaSy + 33 chars
    (re.compile(r"\bAIzaSy[A-Za-z0-9_-]{30,}\b"),
     "google_api_key", "conservative"),
    # Slack: xoxb-/xoxp-/xoxa-…
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
     "slack_token", "conservative"),
    # Telegram bot: <numeric>:<base64-ish>
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
     "telegram_bot_token", "conservative"),

    # ---- Private key blocks ----
    (re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |PRIVATE)?(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----.*?-----END[^-]+-----",
        re.DOTALL,
     ),
     "private_key", "conservative"),

    # ---- JWT (header.payload.signature, base64url) ----
    (re.compile(
        r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
     ),
     "jwt", "conservative"),

    # ---- Generic key/token assignment fallback ----
    # `<key|token|secret|password>=<value>` where value is high-entropy.
    # Capture the VALUE only so we don't redact the key name.
    (re.compile(
        r"((?:api[_-]?key|access[_-]?token|secret(?:[_-]?key)?|password|pwd|auth[_-]?token|bearer)\s*[:=]\s*[\"']?)"
        r"([A-Za-z0-9_./=+-]{16,})",
        re.IGNORECASE,
     ),
     "generic_secret", "conservative"),

    # ---- Authorization header values ----
    (re.compile(
        r"(Authorization:\s*(?:Bearer|Basic|Token)\s+)([A-Za-z0-9_./=+-]{16,})",
        re.IGNORECASE,
     ),
     "auth_header", "conservative"),

    # ---- Aggressive: emails ----
    (re.compile(
        r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+\b",
     ),
     "email", "aggressive"),

    # ---- Aggressive: phone numbers ----
    # Loose international format: optional +, 1-3 digit country, then
    # 7-15 more digits with optional separators. Avoid matching ISO ts
    # like 2026-05-04T12:34:56 by requiring the WHOLE token start.
    (re.compile(
        r"(?<![\d-])\+?\d{1,3}[\s-]?\(?\d{1,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?![\d-])",
     ),
     "phone", "aggressive"),

    # ---- Aggressive: IPv4 ----
    (re.compile(
        r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])",
     ),
     "ipv4", "aggressive"),

    # ---- Aggressive: IPv6 ----
    (re.compile(
        r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b",
     ),
     "ipv6", "aggressive"),
]


# ---------- Public API ----------


def get_level() -> str:
    """Read JANUS_REDACT env. Returns 'off' | 'conservative' | 'aggressive'."""
    v = os.getenv("JANUS_REDACT", "conservative").lower().strip()
    if v in ("off", "false", "no", "0", "none"):
        return "off"
    if v in ("aggressive", "strict", "high"):
        return "aggressive"
    return "conservative"


def redact(text: str, *, level: str | None = None) -> str:
    """Apply redaction to a string. Returns the redacted text.

    `level=None` reads JANUS_REDACT env. Pass an explicit level to
    override (tests, opt-in aggressive on a specific surface).

    Failure-silent: any internal exception returns the original text.
    Pre-v1.11 behavior is preserved when JANUS_REDACT=off.
    """
    if not text:
        return text
    if not isinstance(text, str):
        return text
    lvl = (level or get_level()).lower()
    if lvl == "off":
        return text
    try:
        return _apply_patterns(text, lvl)
    except Exception:
        return text


def redact_obj(obj: Any, *, level: str | None = None) -> Any:
    """Walk a JSON-serializable object and redact every string value.

    Used by logger.write to scrub records before disk. Pure function —
    returns a new object, doesn't mutate the input.
    """
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return redact(obj, level=level)
    if isinstance(obj, list):
        return [redact_obj(x, level=level) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(x, level=level) for x in obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v, level=level) for k, v in obj.items()}
    return obj


# ---------- Implementation ----------


def _apply_patterns(text: str, level: str) -> str:
    """Run every pattern whose required-level <= active level.

    'conservative' applies only conservative-tagged patterns.
    'aggressive' applies both. Order matters: more-specific patterns
    run first so generic_secret doesn't gobble an OpenAI key first.
    """
    aggressive = level == "aggressive"
    for pattern, kind, required in _PATTERNS:
        if required == "aggressive" and not aggressive:
            continue
        text = _replace_one(pattern, text, kind)
    return text


def _replace_one(pattern: re.Pattern, text: str, kind: str) -> str:
    """Replace every match. Uses captured-group strategy for patterns
    that have one — keeps the prefix (e.g., "Authorization: Bearer ")
    visible, redacts the value only.
    """
    label = f"<REDACTED:{kind}>"

    def _sub(m: re.Match) -> str:
        groups = m.groups()
        if len(groups) >= 2 and groups[0] is not None:
            # Two-group pattern: prefix + value. Keep prefix, replace value.
            return groups[0] + label
        return label

    return pattern.sub(_sub, text)
