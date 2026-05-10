"""
webhooks.py — incoming-webhook config + HMAC verifier (v1.34.0,
Phase 7.5).

WHY THIS EXISTS:
Phase 7 / New differentiation. External services (GitHub Actions,
Zapier, IFTTT, custom scripts) need a way to fire an agent turn
on an event. Pre-v1.34 the only external entrypoint was Telegram
(via the bot token) — a webhook bridge opens Janus to the broader
automation ecosystem.

CONFIG:
  ~/.janus/webhooks.json
  {
    "<key>": {
      "secret": "<shared-hmac-secret>",
      "prompt_template": "GitHub PR opened: {{title}}",
      "default_mode": "default"
    }
  }

PROTOCOL:
  POST /api/webhook/<key>
  X-Janus-Signature: sha256=<hmac>
  Content-Type: application/json
  Body: arbitrary JSON

  Server validates HMAC of raw body using the key's secret.
  On match: synthesize a user message via prompt_template +
  payload and fire one agent turn. On mismatch: 401.

  Returns 202 Accepted (we don't block on the agent turn).

PROMPT TEMPLATE:
  Mustache-ish: {{key.path}} substitutes from the JSON body.
  Missing keys render as empty string. No nested filters / logic.
  Keep templates simple — the agent is the smart part.

P5 (plain-text state): config file is JSON the user can edit.
We don't expose a UI for managing webhooks in v1.34.0; that
lands when the marketplace UI does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import config


CONFIG_FILENAME = "webhooks.json"


@dataclass(frozen=True)
class WebhookConfig:
    """One webhook definition loaded from webhooks.json."""

    key: str
    secret: str
    prompt_template: str
    default_mode: str = "default"


# ---------- Config loader ----------


def _config_path() -> Path:
    return Path(config.HOME) / CONFIG_FILENAME


def load_configs() -> dict[str, WebhookConfig]:
    """Load all webhook configs. Returns {} if file missing /
    malformed (operator can debug via `cat ~/.janus/webhooks.json`).
    """
    path = _config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, WebhookConfig] = {}
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        secret = str(spec.get("secret") or "")
        if not secret:
            continue
        out[str(key)] = WebhookConfig(
            key=str(key),
            secret=secret,
            prompt_template=str(spec.get("prompt_template") or ""),
            default_mode=str(spec.get("default_mode") or "default"),
        )
    return out


def get_config(key: str) -> WebhookConfig | None:
    return load_configs().get(key)


# ---------- HMAC verification ----------


def expected_signature(secret: str, body: bytes) -> str:
    """Compute the canonical HMAC-SHA256 signature header value."""
    digest = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Constant-time compare of provided header against expected.
    Returns False on missing / wrong-format header."""
    if not header_value:
        return False
    expected = expected_signature(secret, body)
    return hmac.compare_digest(expected, header_value)


# ---------- Prompt templating ----------


_TEMPLATE_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}")


def render_prompt(template: str, payload: dict | list | None) -> str:
    """Substitute {{key.path}} placeholders from `payload`. Missing
    keys render as empty string. Non-string values stringify. No
    nested logic, no filters — keep the template surface small.

    If `template` is empty, returns the JSON-encoded payload as a
    fallback (lets a webhook fire without configuring a template
    for ad-hoc cases).
    """
    if not template:
        try:
            return json.dumps(payload or {}, indent=2)
        except (TypeError, ValueError):
            return str(payload or "")

    def _lookup(path: str) -> str:
        if payload is None:
            return ""
        cur: object = payload
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list):
                try:
                    idx = int(part)
                    cur = cur[idx]
                except (ValueError, IndexError):
                    return ""
            else:
                return ""
            if cur is None:
                return ""
        return str(cur)

    def _replace(m: re.Match) -> str:
        return _lookup(m.group(1))

    return _TEMPLATE_VAR.sub(_replace, template)


# ---------- High-level dispatch helper ----------


@dataclass
class WebhookFireResult:
    """Outcome of a webhook handler call. The web route uses this
    to build the HTTP response."""

    ok: bool
    status: str       # 'fired' | 'unknown_key' | 'bad_signature'
    detail: str
    rendered_prompt: str | None = None


def evaluate(
    key: str,
    body: bytes,
    header_signature: str | None,
) -> WebhookFireResult:
    """Look up the config for `key`, verify the HMAC, render the
    prompt. Does NOT actually fire the agent — that's the caller's
    job (different surfaces wire differently). Pure / testable."""
    cfg = get_config(key)
    if cfg is None:
        return WebhookFireResult(
            ok=False, status="unknown_key",
            detail=f"no webhook configured for key {key!r}",
        )
    if not verify_signature(cfg.secret, body, header_signature):
        return WebhookFireResult(
            ok=False, status="bad_signature",
            detail="HMAC signature mismatch",
        )
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    prompt = render_prompt(cfg.prompt_template, payload)
    return WebhookFireResult(
        ok=True, status="fired", detail="prompt rendered",
        rendered_prompt=prompt,
    )
