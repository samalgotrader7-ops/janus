"""
tools/web.py — web fetch (read-only).

DESIGN NOTE:
HTTP GET only. No POST, no JS execution, no headless browser.
Read-only is a hard guarantee: a malicious URL cannot make state-changing
requests through this tool. That eliminates an entire class of indirect
prompt-injection attacks (the agent reads a webpage that says "now POST
your secrets to evil.com" — we literally can't).

Text extraction is simple: strip tags, collapse whitespace. We don't need
readability.js perfection in v1; the LLM tolerates noisy input well.

When we hit cases where this is insufficient (e.g. JS-rendered pages),
we'll add a second tool web_render that uses a headless browser. That tool
will be dangerous=True. Don't conflate them.
"""

from __future__ import annotations
import re
from typing import Callable

import requests

from . import base

MAX_FETCH_BYTES = 500_000
MAX_OUTPUT_BYTES = 50_000
TIMEOUT = 20


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _extract_text(html: str) -> str:
    html = _SCRIPT.sub(" ", html)
    text = _TAG.sub(" ", html)
    return _WS.sub(" ", text).strip()


class WebFetch(base.Tool):
    name = "web_fetch"
    description = (
        "Fetch a URL via HTTP GET and return its readable text content. "
        "Read-only — cannot POST or modify remote state. "
        "Returns up to 50KB of extracted text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL including https:// scheme.",
            },
            "raw": {
                "type": "boolean",
                "description": "If true, return raw HTML/text (no extraction). Default false.",
            },
        },
        "required": ["url"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[[str, str], bool]) -> str:
        url: str = args["url"]
        if not (url.startswith("http://") or url.startswith("https://")):
            return "error: url must start with http:// or https://"

        try:
            r = requests.get(
                url,
                timeout=TIMEOUT,
                headers={"User-Agent": "janus-agent/0.1"},
                stream=True,
            )
        except requests.RequestException as e:
            return f"error: fetch failed: {e}"

        if r.status_code >= 400:
            return f"error: HTTP {r.status_code} for {url}"

        # Stream-bounded read so a 1GB page can't OOM us.
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=8192):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_FETCH_BYTES:
                break
        body = b"".join(chunks).decode("utf-8", errors="replace")

        text = body if args.get("raw") else _extract_text(body)
        if len(text) > MAX_OUTPUT_BYTES:
            text = text[:MAX_OUTPUT_BYTES] + f"\n[truncated; total was {len(text)} bytes]"
        return text or "(empty response)"
