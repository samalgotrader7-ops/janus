"""
gateways/web.py — v1.0 chat-shaped local web UI on FastAPI.

WHY:
A web surface for the same `executor.chat()` loop the CLI uses. Same
permission model, same skills, same hooks. No business logic in the
gateway.

SAFETY POSTURE:
- Binds 127.0.0.1 by default. Refuses non-localhost unless the user
  explicitly passes `--host` AND sets `JANUS_WEB_HOST_OK=1` (the env
  var is the deliberate friction).
- The web approver is mode-aware via permissions.decide(). ASK becomes
  DENY because the page has no inline approval UI. Use acceptEdits or
  bypassPermissions via JANUS_APPROVAL, or attach a skill with
  capability tokens, to authorize writes/exec.
- All text is escaped before rendering.

DEPENDENCIES:
- FastAPI is OPTIONAL. Lazy-imported. If missing, `serve()` prints a
  hint instead of crashing the agent.
"""

from __future__ import annotations
import html
import time
import uuid
from typing import Any

from .. import config, executor, logger, memory, skills, hooks, permissions
from .. import branding
from ..tools import default_registry, make_capability_aware, CapabilitySet


_FASTAPI_HINT = (
    "FastAPI not installed. Install with: pip install fastapi uvicorn"
)


def _try_import_fastapi():
    try:
        from fastapi import FastAPI, Body
        from fastapi.responses import HTMLResponse, JSONResponse
        import uvicorn
        return FastAPI, Body, HTMLResponse, JSONResponse, uvicorn
    except ImportError:
        return None


# Per-session conversation state. Keyed by browser-generated session ID.
# In-process only — restart loses sessions. v1.x can persist.
_SESSIONS: dict[str, list[dict]] = {}


def _make_web_approver(mode: str):
    """Mode-aware approver for the web gateway. ASK falls through to DENY
    because there's no inline approval UI yet."""
    def approver(action_label: str, details: str, **kw) -> bool:
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        decision = permissions.decide(risk, mode)
        if decision == permissions.ALLOW:
            return True
        return False  # ASK and DENY both fall to deny.
    return approver


_INDEX_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8" />
<title>janus &mdash; local web UI</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
body { font-family: system-ui, sans-serif; max-width: 820px;
       margin: 24px auto; padding: 16px; color: #222; }
.brand { display: flex; align-items: center; gap: 18px; color: __BRAND__; }
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
#chat { margin: 16px 0; max-height: 60vh; overflow-y: auto;
        border: 1px solid #e0e0e0; border-radius: 6px; padding: 12px;
        background: #fafafa; }
.turn { margin-bottom: 14px; }
.turn .who { font-size: 0.78em; font-weight: 600; color: #666;
             margin-bottom: 4px; text-transform: uppercase;
             letter-spacing: 0.05em; }
.turn.user .who { color: __BRAND__; }
.turn .body { white-space: pre-wrap; font-family: ui-monospace, monospace;
              font-size: 0.92em; line-height: 1.45; }
.turn.assistant .body { color: #222; }
form { display: flex; gap: 8px; }
textarea { flex: 1; height: 5em; font-family: ui-monospace, monospace;
           font-size: 0.95em; padding: 8px;
           border: 1px solid #ccc; border-radius: 4px; }
button { padding: 8px 18px; border-radius: 4px; cursor: pointer;
         border: 1px solid __BRAND__; background: __BRAND__; color: #fff;
         font-weight: 600; }
button:hover { opacity: 0.9; }
.muted { color: #888; font-size: 0.85em; }
.err { color: #a00; }
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
  <span>mode &middot; __MODE__</span>
</p>
<div id="chat"></div>
<form id="form" method="post" action="/chat">
  <textarea name="request" placeholder="message janus..." autofocus></textarea>
  <button type="submit">send</button>
</form>
<script>
const SESSION_ID = (function() {
  let id = sessionStorage.getItem('janus_session');
  if (!id) {
    id = (crypto.randomUUID && crypto.randomUUID()) || Math.random().toString(36).slice(2);
    sessionStorage.setItem('janus_session', id);
  }
  return id;
})();
const chat = document.getElementById('chat');
const form = document.getElementById('form');

function escapeHTML(s) {
  return String(s).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
}
function appendTurn(role, body, isError) {
  const div = document.createElement('div');
  div.className = 'turn ' + role;
  div.innerHTML = '<div class="who">' + role + '</div>' +
                  '<div class="body' + (isError ? ' err' : '') + '">' +
                  escapeHTML(body) + '</div>';
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  const req = (fd.get('request') || '').toString().trim();
  if (!req) return;
  appendTurn('user', req);
  form.querySelector('textarea').value = '';
  const pending = appendTurn('assistant', '...');

  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      body: JSON.stringify({request: req, session_id: SESSION_ID}),
      headers: {'content-type': 'application/json'}
    });
    const data = await resp.json();
    if (data.error) {
      pending.querySelector('.body').textContent = data.error;
      pending.querySelector('.body').classList.add('err');
    } else {
      pending.querySelector('.body').textContent = data.output || '(no output)';
    }
  } catch (e) {
    pending.querySelector('.body').textContent = 'request failed: ' + e;
    pending.querySelector('.body').classList.add('err');
  }
});
</script>
</body></html>
"""


def _index_page() -> str:
    mode = permissions.normalize(config.APPROVAL_MODE)
    return (
        _INDEX_HTML
        .replace("__LOGO_SVG__", branding.svg_logo("currentColor"))
        .replace("__BRAND__", branding.BRAND_COLOR)
        .replace("__VERSION__", branding.VERSION)
        .replace("__TAGLINE__", html.escape(branding.TAGLINE))
        .replace("__MODEL__", html.escape(config.MODEL))
        .replace("__WORKSPACE__", html.escape(str(config.WORKSPACE)))
        .replace("__MODE__", html.escape(mode))
    )


def _build_app():
    deps = _try_import_fastapi()
    if deps is None:
        raise ImportError(_FASTAPI_HINT)
    FastAPI, Body, HTMLResponse, JSONResponse, _uvicorn = deps

    app = FastAPI(title="janus", version=branding.VERSION)

    @app.get("/")
    async def index():
        return HTMLResponse(_index_page())

    @app.get("/favicon.svg")
    async def favicon():
        return HTMLResponse(
            branding.svg_logo(branding.BRAND_COLOR),
            media_type="image/svg+xml",
        )

    @app.post("/chat")
    async def chat(body: dict = Body(default={})):
        if not isinstance(body, dict):
            body = {}
        req = (body.get("request") or "").strip()
        sid = (body.get("session_id") or "").strip() or uuid.uuid4().hex
        if not req:
            return JSONResponse({"error": "empty request"})

        # UserPromptSubmit hook can deny / rewrite.
        try:
            up = hooks.fire(hooks.USER_PROMPT_SUBMIT, {"request": req})
            if not up.allow:
                return JSONResponse({"error": f"blocked by hook: {up.reason}"})
            if up.modified_args and isinstance(up.modified_args.get("request"), str):
                req = up.modified_args["request"]
        except Exception:
            pass

        messages = _SESSIONS.setdefault(sid, [])

        mode = permissions.normalize(config.APPROVAL_MODE)
        base_approver = _make_web_approver(mode)
        caps = CapabilitySet()
        tools = default_registry(capabilities=caps)
        approver = make_capability_aware(base_approver, caps)
        preamble = memory.prepend_for_prompt()

        record: dict[str, Any] = {
            "ts": logger.now_iso(),
            "model": config.MODEL,
            "workspace": str(config.WORKSPACE),
            "request": req,
            "gateway": "web",
            "session_id": sid,
            "mode": mode,
        }

        try:
            t0 = time.time()
            output, trace = executor.chat(
                messages=messages,
                user_input=req,
                tools=tools,
                approver=approver,
                memory_preamble=preamble,
                mode=mode,
                workspace=str(config.WORKSPACE),
                tool_count=len(tools.names()),
                skill_count=len(skills.list_skills()),
                stream=False,
            )
            record["execute_ms"] = int((time.time() - t0) * 1000)
            record["output"] = output
            record["trace"] = trace
        except Exception as e:
            record["error"] = f"execute: {e}"
            logger.write(record)
            return JSONResponse({"error": str(e)})

        logger.write(record)
        try:
            hooks.fire(hooks.STOP, {"request": req, "output": output})
        except Exception:
            pass
        return JSONResponse({"output": output, "session_id": sid})

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
