"""
tools/web_search.py — Phase 9: web search via Brave Search API.

Provider via JANUS_WEB_SEARCH (default 'brave'). API key via
JANUS_BRAVE_API_KEY. If the key is missing the tool returns a clear
error instead of crashing — callers see "configure JANUS_BRAVE_API_KEY"
and can act on it.

We do NOT pull a search SDK in (P6). Plain requests.get against the
Brave HTTPS endpoint, with bounded timeout and capped result count.
"""

from __future__ import annotations
from typing import Callable

import requests

from . import base
from .. import config


_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class WebSearch(base.Tool):
    name = "web_search"
    description = (
        "Search the web. Default provider: Brave Search (set "
        "JANUS_BRAVE_API_KEY). Returns up to 10 results with title, "
        "URL, and a short description."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "count": {
                "type": "integer",
                "description": "Max results (default 10, max 20).",
            },
        },
        "required": ["query"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        provider = (config.WEB_SEARCH_PROVIDER or "brave").lower()
        query = str(args.get("query") or "").strip()
        if not query:
            return "error: query is required"
        count = min(max(int(args.get("count") or 10), 1), 20)

        if provider == "brave":
            return _brave(query, count)
        return f"error: unknown web search provider: {provider!r}"


def _brave(query: str, count: int) -> str:
    if not config.BRAVE_API_KEY:
        return (
            "error: web_search (brave) requires JANUS_BRAVE_API_KEY env var. "
            "Get a free key at https://api.search.brave.com/."
        )
    try:
        r = requests.get(
            _BRAVE_ENDPOINT,
            params={"q": query, "count": count},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": config.BRAVE_API_KEY,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except requests.HTTPError as e:
        return f"error: HTTP {r.status_code} from Brave: {e}"
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return f"(no web results for '{query}')"

    out: list[str] = []
    for i, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        desc = (item.get("description") or "").strip().replace("\n", " ")
        out.append(f"[{i}] {title}")
        out.append(f"    {url}")
        if desc:
            out.append(f"    {desc[:200]}")
    return "\n".join(out)
