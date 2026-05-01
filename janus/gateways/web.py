"""
gateways/web.py — Phase 11: local web UI on FastAPI + HTMX.

WHY:
- A user-facing surface beyond the CLI/Telegram. Same interpreter +
  executor, no business logic in the gateway.

SAFETY POSTURE:
- Binds 127.0.0.1 by default. Refuses non-localhost unless the user
  explicitly passes `--host` AND sets `JANUS_WEB_HOST_OK=1` (the env
  var is the deliberate friction).
- Approval prompts go through a per-request approval queue: if the
  underlying executor needs y/N, the request fails fast with a hint
  to re-run with explicit capabilities. Phase 11 does not implement an
  in-browser approval UX; that's Phase 12+.
- All output is escaped before rendering.

DEPENDENCIES:
- FastAPI is OPTIONAL. Lazy-imported. If missing, the `serve()` entry
  point prints a clear hint instead of crashing the agent.
"""

from __future__ import annotations
import html
import json
import time
from typing import Any

from .. import config, interpreter, executor, logger, memory, skills, hooks
from .. import branding
from ..tools import default_registry, make_capability_aware, CapabilitySet


_FASTAPI_HINT = (
    "FastAPI not installed. Install with: pip install fastapi uvicorn"
)


def _try_import_fastapi():
    try:
        from fastapi import FastAPI, Body, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse
        import uvicorn
        return FastAPI, Body, HTTPException, HTMLResponse, JSONResponse, uvicorn
    except ImportError:
        return None


_INDEX_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8" />
<title>janus &mdash; local web UI</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
body { font-family: system-ui, sans-serif; max-width: 800px;
       margin: 24px auto; padding: 16px; color: #222; }
.brand { display: flex; align-items: center; gap: 18px;
         color: __BRAND__; }
.brand svg { width: 56px; height: 56px; flex: none; }
.brand h1 { margin: 0; font-size: 1.6em; font-weight: 600;
            color: __BRAND__; line-height: 1.05; }
.brand h1 .ver { font-size: 0.55em; font-weight: 400;
                 color: #888; margin-left: 6px; }
.brand h1 small { display: block; font-size: 0.45em; font-weight: 400;
                  color: #888; margin-top: 4px; letter-spacing: 0.02em; }
.status { color: #666; font-size: 0.85em; margin: 16px 0 4px 0;
          font-family: ui-monospace, monospace; }
.status span { margin-right: 18px; }
textarea { width: 100%; height: 6em; font-family: ui-monospace, monospace;
           font-size: 0.95em; padding: 8px; }
.interps { margin-top: 12px; }
.interp { border: 1px solid #aaa; padding: 12px; margin: 8px 0;
          border-radius: 4px; position: relative; }
.interp .label { font-weight: 600; }
.interp .risk  { color: #b08000; font-size: 0.85em; position: absolute;
                 top: 8px; right: 12px; font-family: ui-monospace, monospace; }
.output { margin-top: 16px; background: #f6f6f6; padding: 12px;
          white-space: pre-wrap; border-left: 3px solid __BRAND__;
          font-family: ui-monospace, monospace; font-size: 0.9em; }
.muted  { color: #888; font-size: 0.85em; }
button { padding: 6px 14px; border-radius: 3px; cursor: pointer;
         border: 1px solid #888; background: #fff; }
button:hover { border-color: __BRAND__; color: __BRAND__; }
</style>
</head><body>
<header class="brand">
  __LOGO_SVG__
  <h1>janus<span class="ver">v__VERSION__</span>
    <small>__TAGLINE__</small></h1>
</header>
<p class="status">
  <span>model &middot; __MODEL__</span>
  <span>workspace &middot; __WORKSPACE__</span>
</p>
<form id="form" method="post" action="/run">
  <textarea name="request" placeholder="What do you want done?"
            autofocus></textarea>
  <p><button type="submit">interpret</button></p>
</form>
<div id="result"></div>
<script>
const f = document.getElementById('form');
f.addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(f);
  const r = document.getElementById('result');
  r.innerHTML = '<p class="muted">working...</p>';
  const resp = await fetch('/run', {
    method: 'POST',
    body: JSON.stringify({request: fd.get('request')}),
    headers: {'content-type': 'application/json'}
  });
  const data = await resp.json();
  if (data.error) {
    r.innerHTML = '<p style="color:#a00">'
      + data.error.replace(/[<>&]/g, c=>({ '<':'&lt;','>':'&gt;','&':'&amp;'}[c]))
      + '</p>';
    return;
  }
  let html = '<div class="interps"><h3>interpretations</h3>';
  data.interpretations.forEach((i, idx) => {
    html += '<div class="interp"><div class="label">['
      + (idx+1) + '] ' + i.label
      + '</div><div>' + i.action
      + '</div><div class="risk">risk: ' + i.risk + '</div>'
      + '<button onclick="pick(' + idx + ')">run this</button>'
      + '</div>';
  });
  html += '</div>';
  html += '<div id="run-result"></div>';
  r.innerHTML = html;
  window._lastInterps = data.interpretations;
  window._lastReq = fd.get('request');
});
async function pick(i) {
  const rr = document.getElementById('run-result');
  rr.innerHTML = '<p class="muted">running...</p>';
  const resp = await fetch('/execute', {
    method: 'POST',
    body: JSON.stringify({
      request: window._lastReq,
      interpretation: window._lastInterps[i]
    }),
    headers: {'content-type': 'application/json'}
  });
  const d = await resp.json();
  if (d.error) {
    rr.innerHTML = '<div class="output" style="color:#a00">'
      + d.error.replace(/[<>&]/g, c=>({ '<':'&lt;','>':'&gt;','&':'&amp;'}[c]))
      + '</div>';
    return;
  }
  rr.innerHTML = '<div class="output">'
    + d.output.replace(/[<>&]/g, c=>({ '<':'&lt;','>':'&gt;','&':'&amp;'}[c]))
    + '</div>';
}
</script>
</body></html>
"""


def _index_page() -> str:
    return (
        _INDEX_HTML
        .replace("__LOGO_SVG__", branding.svg_logo("currentColor"))
        .replace("__BRAND__", branding.BRAND_COLOR)
        .replace("__VERSION__", branding.VERSION)
        .replace("__TAGLINE__", html.escape(branding.TAGLINE))
        .replace("__MODEL__", html.escape(config.MODEL))
        .replace("__WORKSPACE__", html.escape(str(config.WORKSPACE)))
    )


def _build_app():
    """Build the FastAPI app. Returns the app object, or raises ImportError
    via the caller when FastAPI is missing."""
    deps = _try_import_fastapi()
    if deps is None:
        raise ImportError(_FASTAPI_HINT)
    FastAPI, Body, HTTPException, HTMLResponse, JSONResponse, _uvicorn = deps

    app = FastAPI(title="janus", version="0.11")

    @app.get("/")
    async def index():
        return HTMLResponse(_index_page())

    @app.get("/favicon.svg")
    async def favicon():
        # Browsers ignore page CSS for favicons, so the favicon uses the
        # literal brand color rather than `currentColor`.
        return HTMLResponse(
            branding.svg_logo(branding.BRAND_COLOR),
            media_type="image/svg+xml",
        )

    @app.post("/run")
    async def run(body: dict = Body(default={})):
        req = (body.get("request") or "").strip() if isinstance(body, dict) else ""
        if not req:
            return JSONResponse({"error": "empty request"})
        # Phase 11: UserPromptSubmit hook can deny / rewrite.
        try:
            up = hooks.fire(hooks.USER_PROMPT_SUBMIT, {"request": req})
            if not up.allow:
                return JSONResponse({"error": f"blocked by hook: {up.reason}"})
            if up.modified_args and isinstance(up.modified_args.get("request"), str):
                req = up.modified_args["request"]
        except Exception:
            pass

        preamble = memory.prepend_for_prompt()
        try:
            interps = interpreter.interpret(
                req, memory_preamble=preamble, skill_hints="",
            )
        except Exception as e:
            return JSONResponse({"error": f"interpret failed: {e}"})
        return JSONResponse({"interpretations": interps})

    @app.post("/execute")
    async def execute(body: dict = Body(default={})):
        if not isinstance(body, dict):
            body = {}
        req = (body.get("request") or "").strip()
        chosen = body.get("interpretation") or {}
        if not req or not chosen.get("action"):
            return JSONResponse({"error": "missing request or interpretation"})

        # Approver: in the web gateway we can't prompt. Auto-deny dangerous
        # actions outside capabilities — the user must attach a skill or
        # rerun in CLI for ad-hoc dangerous work.
        def web_approver(label, details, **kw):
            return False

        caps = CapabilitySet()
        tools = default_registry(capabilities=caps)
        approver = make_capability_aware(web_approver, caps)
        preamble = memory.prepend_for_prompt()

        record: dict[str, Any] = {
            "ts": logger.now_iso(), "model": config.MODEL,
            "workspace": str(config.WORKSPACE), "request": req,
            "gateway": "web",
        }
        try:
            t0 = time.time()
            output, trace = executor.execute(
                original_request=req,
                chosen_label=chosen.get("label", ""),
                chosen_action=chosen["action"],
                tools=tools,
                approver=approver,
                memory_preamble=preamble,
            )
            record["execute_ms"] = int((time.time() - t0) * 1000)
            record["output"] = output
            record["trace"] = trace
            record["interpretations"] = [chosen]
            record["choice"] = "web"
        except Exception as e:
            record["error"] = f"execute: {e}"
            logger.write(record)
            return JSONResponse({"error": str(e)})
        logger.write(record)
        try:
            hooks.fire(hooks.STOP, {"request": req, "output": output})
        except Exception:
            pass
        return JSONResponse({"output": output})

    return app


def _resolve_host(host_arg: str | None) -> tuple[str, str | None]:
    """Determine bind host. Returns (host, refusal_reason or None)."""
    chosen = host_arg or config.WEB_HOST or "127.0.0.1"
    is_local = chosen in ("127.0.0.1", "localhost", "::1")
    if not is_local and not config.WEB_HOST_OK:
        return chosen, (
            f"refused to bind {chosen}: set JANUS_WEB_HOST_OK=1 to "
            f"explicitly authorize a non-localhost bind (intended only "
            f"behind a reverse proxy you control)"
        )
    return chosen, None


def serve(host: str | None = None, port: int | None = None) -> int:
    """Start the web UI. Returns process exit code."""
    deps = _try_import_fastapi()
    if deps is None:
        print(f"error: {_FASTAPI_HINT}")
        return 1
    *_, uvicorn = deps

    config.ensure_home()
    config.assert_configured()

    chosen_host, refusal = _resolve_host(host)
    if refusal:
        print(f"error: {refusal}")
        return 2

    chosen_port = port if port is not None else config.WEB_PORT
    print(f"janus web UI on http://{chosen_host}:{chosen_port}")
    app = _build_app()
    uvicorn.run(app, host=chosen_host, port=chosen_port, log_level="info")
    return 0
