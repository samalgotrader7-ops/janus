"""
tools/browser.py — Phase 9: read-only headless Chromium tools.

Each tool spawns a fresh Playwright browser (no persistent profile, no
cookie carry-over) so cross-tool state doesn't accumulate. Heavier than
sharing a context but simpler to reason about. If the user wants
session-stateful browsing, they should expose it as a skill that runs
multiple tool calls in one turn rather than relying on tool-side state.

Playwright is OPTIONAL. If `playwright` is not installed, every tool
returns a clear error message ("playwright not installed; …") rather than
crashing the agent loop. P8 (errors are observations).

WRITES (clicks, typing, form submission) are intentionally OUT of scope
for Phase 9 — those land behind a separate `browser_interact` capability
in a later phase per spec §10.2 / §6.4.
"""

from __future__ import annotations
import base64
from typing import Any, Callable

from . import base
from .. import config


_PLAYWRIGHT_HINT = (
    "playwright not installed; install with "
    "`pip install playwright && playwright install chromium`"
)


def _try_import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return sync_playwright
    except ImportError:
        return None


def _with_browser(url: str, op: Callable[[Any], str]) -> str:
    sync_playwright = _try_import_playwright()
    if sync_playwright is None:
        return f"error: {_PLAYWRIGHT_HINT}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=config.BROWSER_TIMEOUT * 1000,
                )
            except Exception as e:
                browser.close()
                return f"error: navigation failed: {type(e).__name__}: {e}"
            try:
                result = op(page)
            finally:
                browser.close()
            return result
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


class BrowserNavigate(base.Tool):
    name = "browser_navigate"
    description = (
        "Navigate to a URL in headless Chromium and return the page title "
        "and the full rendered text (truncated at 50KB). Read-only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "wait_ms": {"type": "integer", "description": "Extra wait after load (default 500)."},
        },
        "required": ["url"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        url = args.get("url", "")
        wait = max(0, int(args.get("wait_ms") or 500))

        def op(page):
            page.wait_for_timeout(wait)
            title = page.title() or ""
            text = page.evaluate("() => document.body && document.body.innerText || ''")
            if len(text) > 50_000:
                text = text[:50_000] + "\n[truncated]"
            return f"title: {title}\n\n{text}"
        return _with_browser(url, op)


class BrowserText(base.Tool):
    name = "browser_text"
    description = "Fetch a URL via headless Chromium and return only its main text."
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        url = args.get("url", "")

        def op(page):
            text = page.evaluate("() => document.body && document.body.innerText || ''")
            return text[:50_000] + ("\n[truncated]" if len(text) > 50_000 else "")
        return _with_browser(url, op)


class BrowserSnapshot(base.Tool):
    name = "browser_snapshot"
    description = (
        "Fetch a URL via headless Chromium and return a base64-encoded PNG "
        "screenshot of the viewport. Use sparingly — outputs are large."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "full_page": {"type": "boolean", "description": "Whole page (default false)."},
        },
        "required": ["url"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        url = args.get("url", "")
        full = bool(args.get("full_page"))

        def op(page):
            png = page.screenshot(type="png", full_page=full)
            return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
        return _with_browser(url, op)


class BrowserLinks(base.Tool):
    name = "browser_links"
    description = "Fetch a URL and list its links as `[text] -> url` (capped at 200)."
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        url = args.get("url", "")

        def op(page):
            links = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".slice(0, 200)"
                ".map(a => [(a.innerText || '').trim().slice(0, 80), a.href])"
            )
            if not links:
                return "(no links)"
            return "\n".join(f"[{t}] -> {u}" for t, u in links if u)
        return _with_browser(url, op)


class BrowserGetImage(base.Tool):
    name = "browser_get_image"
    description = (
        "Extract the src URL of an <img> matching a CSS selector on a "
        "given page. Returns the resolved URL string."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "selector": {
                "type": "string",
                "description": "CSS selector of the <img>, e.g. 'main img.hero'.",
            },
        },
        "required": ["url", "selector"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        url = args.get("url", "")
        sel = args.get("selector", "")
        if not sel:
            return "error: selector is required"

        def op(page):
            src = page.evaluate(
                "(s) => { const e = document.querySelector(s); "
                "return e && e.tagName === 'IMG' ? e.src : null; }",
                sel,
            )
            if not src:
                return f"(no <img> matched selector: {sel})"
            return src
        return _with_browser(url, op)
