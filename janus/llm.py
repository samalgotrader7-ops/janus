"""
llm.py — minimal LLM client.

DESIGN NOTE:
Two functions only:
  - chat(messages, tools=None, json_mode=False) -> response dict
  - extract_text(response) / extract_tool_calls(response) -> typed accessors

Why not use openai-python or litellm?
  We support ANY OpenAI-compatible endpoint, including weird local ones
  whose pydantic schemas don't quite match. A 50-line raw-requests client
  is more portable than a fat SDK that breaks on minor protocol drift.
  When the project matures we can swap in litellm; for now, simple wins.

This is the executor's bridge to the world. Tool calling format follows the
OpenAI spec (Anthropic, OpenRouter, Mistral, DeepSeek all match it).

v1.4: `model=` parameter overrides config.MODEL per call (used by swarms
to mix cheap/expensive models per role). Retry/backoff wraps the POST to
absorb transient 5xx and ConnectionError — without it, long-running swarms
die on first hiccup.
"""

from __future__ import annotations
import json
import random
import time
from typing import Any

import requests

from . import config


# ---------- Retry / backoff ----------
#
# Used by both chat() and streaming.chat_stream(). Retries on transient
# failures (HTTP 429, HTTP 5xx, ConnectionError, Timeout). Does NOT retry
# on 4xx other than 429 — those are deterministic client errors.


# 529 = Anthropic 'overloaded'. Not in the OpenAI spec but Anthropic
# uses it under load. Treating it as retryable matches Hermes.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


def _backoff_sleep(attempt: int, *, provider_cooldown: float = 0.0) -> None:
    """Exponential backoff with jitter. attempt is 0-indexed (0 = first try).

    Sleep length: max(provider_cooldown, base * 2^attempt + uniform(0, base)).
    The provider_cooldown override (v1.14.0) lets the rate-limit tracker
    feed in 'this provider just rate-limited us, sleep at LEAST X seconds'
    so we don't burn retry attempts hammering a provider that already
    told us to slow down.
    Patched out in tests via monkeypatching `time.sleep`."""
    base = config.LLM_RETRY_BACKOFF_BASE_S
    delay = base * (2 ** attempt) + random.uniform(0, base)
    if provider_cooldown > delay:
        delay = provider_cooldown
    time.sleep(delay)


def _post_with_retry(
    url: str,
    *,
    headers: dict,
    json_payload: dict,
    timeout: int,
    stream: bool = False,
) -> requests.Response:
    """POST with bounded retry on transient failures.

    Returns the requests.Response. On the LAST attempt, returns whatever
    response we got (5xx included) so the caller's `raise_for_status()`
    surfaces the error normally. Raises ConnectionError/Timeout if the
    last attempt also fails to connect.

    v1.14.0: when the rate-limit tracker has a recent 429 cooldown for
    this provider+model, _backoff_sleep honors it as a floor. Avoids
    burning retry attempts hammering a provider that already told us
    to slow down.

    v1.14.0: 529 (Anthropic overloaded) added to retryable set.
    """
    post_kwargs: dict = {
        "headers": headers, "json": json_payload, "timeout": timeout,
    }
    if stream:
        post_kwargs["stream"] = True

    # Pull cooldown hint from rate_limit module (best-effort, no hard dep).
    provider_cooldown = 0.0
    try:
        from . import rate_limit
        provider = _provider_from_base(config.API_BASE)
        model = json_payload.get("model", "")
        provider_cooldown = rate_limit.cooldown_seconds(provider, model)
    except Exception:
        provider_cooldown = 0.0

    # If provider is in cooldown, sleep BEFORE the first attempt — the
    # caller's expectation is "succeed eventually", and trying immediately
    # would just return 429 again.
    if provider_cooldown > 0:
        time.sleep(provider_cooldown)

    last_exc: Exception | None = None
    for attempt in range(config.LLM_RETRY_MAX_ATTEMPTS):
        is_last_attempt = attempt + 1 == config.LLM_RETRY_MAX_ATTEMPTS
        try:
            r = requests.post(url, **post_kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            if is_last_attempt:
                raise
            _backoff_sleep(attempt)
            continue
        if r.status_code in _RETRYABLE_STATUS and not is_last_attempt:
            r.close()
            # Re-pull cooldown — the just-failed call may have set it.
            try:
                from . import rate_limit
                provider = _provider_from_base(config.API_BASE)
                model = json_payload.get("model", "")
                cd = rate_limit.cooldown_seconds(provider, model)
            except Exception:
                cd = 0.0
            _backoff_sleep(attempt, provider_cooldown=cd)
            continue
        return r
    # Unreachable: loop body always returns or raises on last attempt.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop fell through")


def apply_cache_markers(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap select messages in Anthropic-style content blocks with
    `cache_control: ephemeral`. OpenRouter forwards to Anthropic's
    prompt cache; OpenAI ignores extra fields.

    v1.14.0 — marks TWO points:
      1) the LAST system message (always — that's the big static prompt)
      2) the last user message (only if substantial, ≥1024 chars — the
         turn-context block where memory + state introspection live)

    Anthropic allows up to 4 cache breakpoints per message list, so two
    is well under the limit. The 1024-char threshold matches Anthropic's
    minimum cacheable size — anything shorter doesn't save tokens.

    No-op when:
      - JANUS_PROMPT_CACHE is off (default)
      - the message list has no system message
      - the message content is already a list (caller built blocks itself)
    """
    if not config.PROMPT_CACHE_MARKERS:
        return messages

    out: list[dict[str, Any]] = []
    last_system_idx = -1
    last_user_idx = -1
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            last_system_idx = i
        elif m.get("role") == "user":
            last_user_idx = i

    for i, m in enumerate(messages):
        content = m.get("content")
        # Only string content is wrappable — already-list content was
        # constructed by a caller that knows what it's doing.
        if not isinstance(content, str):
            out.append(m)
            continue
        if i == last_system_idx:
            out.append({
                "role": "system",
                "content": [{
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }],
            })
        elif i == last_user_idx and len(content) >= 1024:
            out.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }],
            })
        else:
            out.append(m)
    return out


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    json_mode: bool = False,
    temperature: float = 0.7,
    model: str | None = None,
) -> dict:
    """Single chat completion. Returns the raw 'message' object from the response.

    `model` overrides config.MODEL for this call only. Used by swarm
    sub-agents to mix cheap/expensive models per role.
    """
    url = f"{config.API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }
    chosen_model = model or config.MODEL
    payload: dict[str, Any] = {
        "model": chosen_model,
        "messages": apply_cache_markers(messages),
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    r = _post_with_retry(
        url, headers=headers, json_payload=payload, timeout=config.LLM_TIMEOUT,
    )
    # v1.11.0 — feed the rate-limit tracker BEFORE raise_for_status so
    # 429s are recorded even when the call ultimately fails. raise will
    # then bubble the HTTPError up to the caller (preserving v1.4 retry
    # semantics).
    try:
        from . import rate_limit
        provider = _provider_from_base(config.API_BASE)
        # Tokens 0 if no body yet (429 / 5xx); we'll update on success below.
        rate_limit.record_request(
            provider=provider, model=chosen_model,
            tokens=0,
            ok=(200 <= r.status_code < 300),
            status_429=(r.status_code == 429),
        )
    except Exception:
        pass

    # v1.16.1 — turn opaque 404s into actionable errors.
    # vLLM / Ollama / llama.cpp / vendored OpenAI-compat servers return 404
    # when the requested model id isn't loaded. The bare requests.HTTPError
    # message ("404 Client Error: Not Found for url: ...") is useless to the
    # user. Common cause: an OpenRouter-style 'openai/Foo/Bar' model id
    # configured against a direct vLLM endpoint that knows the model as
    # 'Foo/Bar'. We detect that shape and suggest the fix.
    if r.status_code == 404:
        raise _explain_404(chosen_model, config.API_BASE, r)

    r.raise_for_status()
    body = r.json()
    # Phase 13: feed token usage to the cost tracker. Non-fatal — providers
    # that omit `usage` (some local backends) are silently treated as zero.
    try:
        from . import cost
        cost.record(chosen_model, body.get("usage"))
    except Exception:
        pass
    # v1.11.0 — also report token count to the rate tracker on success.
    try:
        from . import rate_limit
        usage = body.get("usage") or {}
        total = int(usage.get("total_tokens") or 0)
        if total:
            rate_limit.record_request(
                provider=_provider_from_base(config.API_BASE),
                model=chosen_model, tokens=total, ok=True,
            )
    except Exception:
        pass
    return body["choices"][0]["message"]


def _provider_from_base(api_base: str) -> str:
    """Best-effort provider name from API_BASE host. Used for grouping
    rate-limit / cost data without forcing the user to declare it."""
    try:
        from urllib.parse import urlparse
        host = urlparse(api_base).netloc.lower()
    except Exception:
        return "unknown"
    if "openrouter" in host:
        return "openrouter"
    if "anthropic" in host:
        return "anthropic"
    if "openai" in host:
        return "openai"
    if "ollama" in host or "localhost" in host or "127.0.0.1" in host:
        return "local"
    return host or "unknown"


# ---------- Helpful errors + endpoint introspection (v1.16.1) ----------


def _explain_404(model: str, api_base: str, response: requests.Response) -> RuntimeError:
    """Build an actionable error message for a 404 from /chat/completions.

    Includes:
      - what was tried (URL + model)
      - the most likely cause (provider-prefix mismatch on vLLM-shaped endpoint)
      - a concrete suggestion (the unprefixed model name)
      - how to verify (curl /v1/models)
    """
    base = api_base.rstrip("/")
    provider = _provider_from_base(api_base)
    hints: list[str] = []

    parts = model.split("/")
    if len(parts) >= 2 and provider not in ("openrouter",):
        # 'openai/Foo' / 'meta/Bar' style prefix on a direct endpoint.
        # The first component looks like a namespace the endpoint doesn't
        # use. Suggest stripping it.
        unprefixed = "/".join(parts[1:])
        hints.append(
            f"the prefix {parts[0]!r}/ looks like an OpenRouter-style "
            f"namespace. {provider} endpoints usually serve the model as "
            f"just {unprefixed!r} (no prefix). Try setting "
            f"JANUS_MODEL={unprefixed}"
        )

    hints.append(
        f"list what this endpoint actually serves: "
        f"`curl {base}/models`"
    )

    # Try to pull the server's error body too — vLLM sometimes lists the
    # available models in the 404 response. Best-effort.
    server_msg = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            err = body.get("error") or body.get("detail") or body
            if isinstance(err, dict):
                server_msg = err.get("message") or err.get("msg") or ""
            elif isinstance(err, str):
                server_msg = err
    except Exception:
        pass
    if not server_msg:
        # Fall back to a snippet of raw text (capped).
        try:
            server_msg = (response.text or "")[:300].strip()
        except Exception:
            server_msg = ""

    msg_parts = [
        f"404 from {base}/chat/completions for model {model!r} — the "
        f"endpoint doesn't recognize this model id.",
    ]
    if server_msg:
        msg_parts.append(f"  server said: {server_msg}")
    msg_parts.append("  try:")
    for h in hints:
        msg_parts.append(f"    - {h}")
    return RuntimeError("\n".join(msg_parts))


def list_models(api_base: str | None = None,
                api_key: str | None = None,
                timeout: int = 10) -> list[str]:
    """Probe `<base>/models` and return the model ids the endpoint serves.

    Returns [] on any failure (network, auth, malformed body). Used by
    `janus doctor` to surface "what's actually loaded over there."
    """
    base = (api_base or config.API_BASE).rstrip("/")
    key = api_key if api_key is not None else config.API_KEY
    try:
        r = requests.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=timeout,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    # OpenAI shape: {"data": [{"id": "...", ...}, ...]}
    items = data.get("data") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict):
        items = data.get("models")
    if items is None and isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for m in items:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                out.append(str(mid))
        elif isinstance(m, str):
            out.append(m)
    return out


def chat_stream(messages, tools=None, temperature=0.7, model: str | None = None):
    """Re-export of streaming.chat_stream so callers don't need to import
    a second module just to switch modes. See streaming.py for shape.

    `model` overrides config.MODEL for this call only.
    """
    from . import streaming
    return streaming.chat_stream(
        messages, tools=tools, temperature=temperature, model=model,
    )


def parse_json_loose(raw: str) -> Any:
    """Accept JSON even if the model wrapped it in markdown fences.

    Some smaller models ignore response_format=json_object and emit fenced
    blocks anyway. We strip the fences rather than fail.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw.strip())
