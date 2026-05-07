"""
model_fallback.py — multi-model fall-through (v1.28.3).

When the primary model errors hard (5xx after retries exhausted /
ConnectionError / Timeout), Janus can transparently try the next
model in a configured chain. Saves the user from "provider is
flaky" turns and enables cheap-model-first / strong-model-second
patterns.

CONFIGURATION:

  ``JANUS_MODEL_FALLBACK=model_a,model_b``

  Comma-separated. The PRIMARY model (``config.MODEL`` or the
  ``model=`` arg) is tried first; failures fall through to the
  configured chain in order. Empty / unset = no fallback (current
  behavior).

WHAT TRIGGERS A FALL-THROUGH:

  * ``requests.exceptions.ConnectionError`` — DNS / refused / network
  * ``requests.exceptions.Timeout`` — request timed out
  * ``requests.HTTPError`` with status_code 5xx — server-side issue

WHAT DOES NOT TRIGGER:

  * 4xx client errors — same input on a different model probably
    won't help (auth, context-length, malformed body, unknown model
    name)
  * 429 — handled by rate-limit retry backoff in _post_with_retry;
    falling through to a DIFFERENT model just shifts the rate-limit
    problem
  * Successful responses — even if the body is empty / weird,
    fall-through here is too risky (we'd lose token-spend audit)

Quality-based fall-through ("model said 'I don't know' too many
times") is deliberately deferred — that's a v1.28.x or v1.29.x
candidate. v1.28.3 is the infra-failure path only.

DOES NOT APPLY TO STREAMING:

``llm.chat_stream`` is not wrapped in v1.28.3. Streaming fall-through
needs partial-output discard + reconnect logic that's its own
release. Streaming callers fail loud (current behavior); ``chat()``
callers fall through.

EVENT VOCABULARY:

  ``model_fallback`` — fired when the primary attempt fails and we
  switch to the next model in the chain. Includes from_model,
  to_model, reason fields.
"""

from __future__ import annotations

import os

from . import config


def parse_chain(primary: str) -> list[str]:
    """Build the ordered list of models to try.

    Always starts with ``primary``; appends the env var chain if
    configured. Deduplicates while preserving order — listing the
    primary explicitly in JANUS_MODEL_FALLBACK doesn't double up.
    """
    chain: list[str] = []
    if primary:
        chain.append(primary)

    raw = os.environ.get("JANUS_MODEL_FALLBACK", "").strip()
    if raw:
        for token in raw.split(","):
            t = token.strip()
            if t and t not in chain:
                chain.append(t)
    return chain


def is_fallback_trigger(exc: BaseException) -> bool:
    """Is this exception one we should fall through on?

    Conservative: only infra-shaped failures. 4xx client errors are
    NOT triggers — switching model won't fix them.
    """
    # Lazy import — keep tests that don't have requests installed
    # (none today, but just in case) from blowing up on module load.
    try:
        import requests
    except ImportError:
        return False

    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None and 500 <= resp.status_code < 600:
            return True
        return False
    return False


def reason_string(exc: BaseException) -> str:
    """Short human-readable label for the fallback event ('5xx',
    'connection_error', 'timeout', or the raw exception class name)."""
    try:
        import requests
    except ImportError:
        return type(exc).__name__

    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None:
            return f"{resp.status_code}"
    return type(exc).__name__


def record_fallback(*, from_model: str, to_model: str, reason: str) -> None:
    """Append a model_fallback row to log.jsonl. Best-effort, no
    raise. Surfaces' on_step renderers can also fire a
    ``model_fallback`` event from this same data shape if they want
    a live UI cue."""
    try:
        from . import logger as _logger
        _logger.write({
            "ts": _logger.now_iso(),
            "type": "model_fallback",
            "from_model": from_model,
            "to_model": to_model,
            "reason": reason,
        })
    except Exception:
        pass


__all__ = [
    "parse_chain",
    "is_fallback_trigger",
    "reason_string",
    "record_fallback",
]
