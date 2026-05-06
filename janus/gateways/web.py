"""
gateways/web.py — v1.3 chat-shaped local web UI on FastAPI.

WHY:
A web surface for the same `executor.chat()` loop the CLI uses. Same
permission model, same skills, same hooks. No business logic here.

v1.3 ADDITIONS:
- Optional pairing (off by default since localhost is implicit-trust).
  Set JANUS_WEB_PAIRING=1 when binding non-localhost behind a proxy.
- POST /home and POST /memory endpoints for parity with Telegram.
- Self-introduction loaded from soul.md + user.md on first message
  per browser session.
- Persistent sessions via gw.GatewaySession — survives restart.

DEFERRED to v1.4 (with explicit notes in the v1.3 release):
- SSE streaming (currently full-response only). Indicator events fire
  but render after the turn completes.
- Inline approval UI. ASK still falls through to DENY in the web
  approver. v1.3 L3 introduces "approval routed to home channel" —
  use Telegram for approvals when chatting via web.

SAFETY POSTURE:
- Binds 127.0.0.1 by default. Refuses non-localhost unless the user
  explicitly passes `--host` AND sets `JANUS_WEB_HOST_OK=1`.
- All text is escaped before rendering.

DEPENDENCIES:
- FastAPI is OPTIONAL. Lazy-imported. If missing, `serve()` prints a
  hint instead of crashing the agent.
"""

from __future__ import annotations
import html
import os
import time
import uuid
from typing import Any

from .. import config, executor, logger, memory, skills, hooks, permissions
from .. import branding, cost
from ..tools import default_registry, make_protected, CapabilitySet
from . import _common as gw
from . import web_auth, web_audit

# v1.21: FastAPI types must be visible at module-level so route function
# annotations resolve under `from __future__ import annotations`. When
# FastAPI isn't installed we set placeholders — `_try_import_fastapi`
# is the runtime gate that prevents anyone calling _build_app().
try:
    from fastapi import Request as _FastAPIRequest
    from fastapi.responses import RedirectResponse as _FastAPIRedirectResponse
    Request = _FastAPIRequest
    RedirectResponse = _FastAPIRedirectResponse
except ImportError:
    Request = Any  # type: ignore[assignment,misc]
    RedirectResponse = None  # type: ignore[assignment]

GATEWAY_NAME = "web"


def _pairing_required() -> bool:
    """When binding non-localhost, require pairing unless explicitly off."""
    return os.environ.get("JANUS_WEB_PAIRING", "").lower() in ("1", "true", "yes")


def _localhost_no_auth() -> bool:
    """v1.21: opt-in escape hatch for localhost-only deployments where
    the operator has decided HTTP auth is unnecessary (e.g., dev VM
    bound to 127.0.0.1, single-user laptop). OFF by default — even
    localhost requires auth in v1.21+ unless this is set."""
    return os.environ.get("JANUS_WEB_LOCALHOST_NO_AUTH", "").lower() in (
        "1", "true", "yes",
    )


def _is_localhost_request(client_host: str) -> bool:
    return client_host in ("127.0.0.1", "::1", "localhost", "testclient")


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


# v1.3: in-process cache backed by persistent gw.GatewaySession on disk.
# Kept as a dict facade for backward-compat with existing tests.
_SESSIONS: dict[str, list[dict]] = {}


def _load_or_create_session(sid: str) -> list[dict]:
    """Return the messages list for a session, loading from disk if needed."""
    if sid in _SESSIONS:
        return _SESSIONS[sid]
    persisted = gw.load_session(GATEWAY_NAME, sid)
    _SESSIONS[sid] = persisted.messages
    return _SESSIONS[sid]


def _persist_session(sid: str, messages: list[dict], mode: str) -> None:
    """Write the in-memory messages list back to disk."""
    sess = gw.load_session(GATEWAY_NAME, sid)
    sess.messages = messages
    sess.mode = mode
    gw.save_session(sess)


def _web_interview_handle(sid: str, arg: str) -> str:
    """v1.19.1 — handle /interview <subcommand> on the web gateway.

    Subcommands match the CLI / Telegram versions:
      (no arg)               enable drip mode (10 q/day), no category filter
      <category>             drip filtered to <category>
      daily [N]              slow drip (N q/day, default 2)
      pause                  stop drip
      about-me               render current memory snapshot

    Returns plain-text output for the chat UI to display.
    """
    from .. import interviews as _iv
    _iv.maybe_install_bundled()
    state = _iv.load_state("web", sid)

    arg = (arg or "").strip()
    arg_low = arg.lower()

    if arg_low in ("pause", "stop"):
        state.mode = "idle"
        state.drip_filter_category = ""
        state.current_question_id = ""
        _iv.save_state(state)
        return "interview paused."

    if arg_low in ("about-me", "aboutme", "about me"):
        return _web_render_about_me()

    category_filter = ""
    per_day = 10
    suffix = " (about all categories)"
    if arg_low.startswith("daily"):
        rest = arg[5:].strip()
        try:
            per_day = max(1, min(20, int(rest))) if rest else _iv.DRIP_DEFAULT_PER_DAY
        except ValueError:
            per_day = _iv.DRIP_DEFAULT_PER_DAY
        suffix = " (slow drip)"
    elif arg_low in _iv.SUPPORTED_CATEGORIES:
        category_filter = arg_low
        suffix = f" about {arg_low}"
    elif arg_low:
        return (
            f"usage: /interview [<category>|daily [N]|pause|about-me]\n"
            f"category: {', '.join(_iv.SUPPORTED_CATEGORIES)}"
        )

    state.mode = "drip"
    state.drip_filter_category = category_filter
    if not state.started_at:
        state.started_at = _iv._now_iso()
    _iv.reset_drip_quota(state, per_day=per_day)
    _iv.save_state(state)

    return (
        f"🎯 interview mode on — Janus will ask up to {per_day} "
        f"question(s)/day{suffix}.\n\n"
        f"Reply normally to answer, 'skip' to skip, 'stop drip' to pause."
    )


def _web_render_about_me() -> str:
    """Plain-text 'about me' for web UI."""
    from .. import interviews as _iv, memory_index, memory_cards
    try:
        memory_index.reconcile()
    except Exception:
        pass
    rows = memory_index.list_all()
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)

    parts = ["**Here's what I know about you:**", ""]
    any_cards = False
    for cat in _iv.SUPPORTED_CATEGORIES:
        cat_rows = by_type.get(cat, [])
        if not cat_rows:
            continue
        any_cards = True
        parts.append(f"**{cat}**")
        from pathlib import Path
        for r in cat_rows[:10]:
            try:
                card = memory_cards.read_card(Path(r["path"]))
                content = card.content[:200].replace("\n", " ")
                parts.append(f"- {r['subject']}: {content}")
            except Exception:
                continue
        parts.append("")
    if not any_cards:
        parts.append("_(nothing yet — try /interview to fill in your profile)_")
    else:
        parts.append("_anything wrong? reply with corrections._")
    return "\n".join(parts)


def _make_web_approver(mode: str):
    """Mode-aware approver for the web gateway.

    v1.3: ASK still falls through to DENY because the page has no inline
    approval UI. The Layer 3 'approval routed to home channel' feature
    will let you approve from Telegram when chatting via web. Until then,
    use acceptEdits / bypassPermissions, or attach a skill with capability
    tokens.
    """
    def approver(action_label: str, details: str, **kw) -> bool:
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        decision = permissions.decide(risk, mode)
        if decision == permissions.ALLOW:
            return True
        return False  # ASK and DENY both fall to deny (L3 will bridge).
    return approver


_LOGIN_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8" />
<title>janus &mdash; sign in</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
body { font-family: system-ui, sans-serif; max-width: 460px;
       margin: 8vh auto; padding: 24px; color: #222; }
.brand { display: flex; align-items: center; gap: 14px;
         color: __BRAND__; margin-bottom: 32px; }
.brand svg { width: 40px; height: 40px; flex: none; }
.brand h1 { margin: 0; font-size: 1.3em; font-weight: 600;
            color: __BRAND__; }
form { display: flex; flex-direction: column; gap: 12px; }
label { font-size: 0.92em; color: #555; }
input[type="password"] { font-family: ui-monospace, monospace;
                         padding: 10px; border: 1px solid #ccc;
                         border-radius: 4px; font-size: 0.92em; }
button { padding: 10px 18px; border-radius: 4px; cursor: pointer;
         border: 1px solid __BRAND__; background: __BRAND__;
         color: #fff; font-weight: 600; font-size: 0.95em; }
button:hover { opacity: 0.9; }
.muted { color: #888; font-size: 0.85em; line-height: 1.5; }
.err { color: #a00; padding: 8px 12px; background: #fee;
       border: 1px solid #fcc; border-radius: 4px;
       margin-bottom: 12px; font-size: 0.9em; }
.help { margin-top: 24px; font-size: 0.85em; color: #666;
        line-height: 1.6; }
code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px;
       font-family: ui-monospace, monospace; }
</style>
</head><body>
<header class="brand">
  __LOGO_SVG__
  <h1>janus &mdash; sign in</h1>
</header>
__ERROR_BLOCK__
<form method="post" action="/login">
  <label>Bootstrap token</label>
  <input type="password" name="token" autofocus required
         placeholder="paste from ~/.janus/web_token" />
  <button type="submit">sign in</button>
</form>
<div class="help">
The bootstrap token is in <code>~/.janus/web_token</code> on the
server. The first time <code>janus web</code> starts it prints the
token to the console; you can also <code>cat</code> the file directly.
Rotate with <code>janus web rotate-token</code>.
</div>
</body></html>
"""


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
<meta name="csrf-token" content="__CSRF_TOKEN__">
<header class="brand">
  __LOGO_SVG__
  <h1>janus<span class="ver">v__VERSION__</span>
    <small>__TAGLINE__</small></h1>
  <button id="logout" type="button"
          style="margin-left:auto; background:#fff; color:__BRAND__;
                 border:1px solid __BRAND__; padding:6px 12px;
                 font-size:0.8em; font-weight:600; border-radius:4px;
                 cursor:pointer;">sign out</button>
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
const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]').content;
document.getElementById('logout').addEventListener('click', async () => {
  await fetch('/logout', {method: 'POST', credentials: 'same-origin'});
  window.location = '/login';
});
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
      headers: {
        'content-type': 'application/json',
        'x-csrf-token': CSRF_TOKEN
      },
      credentials: 'same-origin'
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


def _index_page(csrf_token: str = "") -> str:
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
        .replace("__CSRF_TOKEN__", html.escape(csrf_token))
    )


def _login_page(error: str = "") -> str:
    err_block = ""
    if error:
        err_block = f'<div class="err">{html.escape(error)}</div>'
    return (
        _LOGIN_HTML
        .replace("__LOGO_SVG__", branding.svg_logo("currentColor"))
        .replace("__BRAND__", branding.BRAND_COLOR)
        .replace("__ERROR_BLOCK__", err_block)
    )


# v1.21: helper to extract the client IP. FastAPI's request.client may
# be None in some contexts; we coerce to a stable string.
def _client_ip(request) -> str:
    try:
        if request.client and request.client.host:
            return request.client.host
    except Exception:
        pass
    return "unknown"


def _check_auth(request) -> tuple[str | None, str | None]:
    """v1.21 auth gate. Returns (sid, error). If sid is non-None the
    request is authenticated; if error is non-None the caller must
    respond 401 with that message.

    Localhost-only deployments can opt out via JANUS_WEB_LOCALHOST_NO_AUTH=1.
    Other binds always require a signed session cookie.
    """
    client_host = _client_ip(request)
    if _is_localhost_request(client_host) and _localhost_no_auth():
        return ("__localhost_no_auth__", None)
    cookie = request.cookies.get(web_auth.cookie_name())
    sid = web_auth.verify_session(cookie or "")
    if not sid:
        return (None, "authentication required")
    return (sid, None)


def _check_csrf(request, sid: str) -> bool:
    """v1.21: validate the X-CSRF-Token header against the session.

    For non-mutating GETs we don't bother checking. Localhost-no-auth
    sessions skip CSRF too (the bypass implies operator trust).
    """
    if sid == "__localhost_no_auth__":
        return True
    token = request.headers.get("x-csrf-token", "")
    return web_auth.verify_csrf(sid, token)


def _build_app():
    deps = _try_import_fastapi()
    if deps is None:
        raise ImportError(_FASTAPI_HINT)
    FastAPI, Body, HTMLResponse, JSONResponse, _uvicorn = deps
    # Request / RedirectResponse are imported at module-level so route
    # function annotations resolve under `from __future__ import annotations`.

    app = FastAPI(title="janus", version=branding.VERSION)

    # ---------- v1.21: unauthenticated routes (login + healthz + assets) ----------

    @app.get("/healthz")
    async def healthz():
        # Cheap liveness probe. Intentionally returns no version /
        # workspace info — that would leak deployment metadata to
        # unauthenticated callers.
        return JSONResponse({"status": "ok"})

    @app.get("/favicon.svg")
    async def favicon():
        return HTMLResponse(
            branding.svg_logo(branding.BRAND_COLOR),
            media_type="image/svg+xml",
        )

    @app.get("/login")
    async def login_page(request: Request, error: str = ""):
        # If already authenticated, send the user to the index instead
        # of re-prompting.
        sid, _err = _check_auth(request)
        if sid is not None:
            return RedirectResponse(url="/", status_code=303)
        return HTMLResponse(_login_page(error=error))

    @app.post("/login")
    async def login_post(request: Request):
        # Body type is determined by content-type header. JSON for the
        # JS fetch() flow; form-urlencoded for the no-JS HTML form.
        # Read both manually so neither path 422s.
        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "")[:200]

        # Login throttle — block hammering even before checking the token.
        blocked, retry_after = web_auth.is_ip_blocked(ip)
        if blocked:
            web_audit.login_failure(
                ip, reason=f"blocked_ip ({retry_after}s remaining)", ua=ua,
            )
            return JSONResponse(
                {"error": f"too many failed attempts; try again in {retry_after}s"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        # Rate-limit "auth" route class to prevent brute-force at high
        # concurrency from one IP.
        ok, ra = web_auth.rate_limit_take(ip, "auth")
        if not ok:
            return JSONResponse(
                {"error": "rate limited"},
                status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            )

        token = ""
        is_form = False
        # Try JSON body first (the JS fetch() flow). If that fails, fall
        # back to form-urlencoded (the no-JS HTML form fallback).
        try:
            body = await request.json()
            if isinstance(body, dict):
                token = (body.get("token") or "").strip()
        except Exception:
            body = None
        if not token:
            try:
                form = await request.form()
                token = (form.get("token") or "").strip()
                if token:
                    is_form = True
            except Exception:
                pass

        if not web_auth.verify_bootstrap_token(token):
            web_auth.record_login_attempt(ip, success=False)
            web_audit.login_failure(ip, reason="bad_token", ua=ua)
            if is_form:
                # Render login page with error so the user sees feedback.
                return HTMLResponse(
                    _login_page(error="invalid token"),
                    status_code=401,
                )
            return JSONResponse(
                {"error": "invalid token"}, status_code=401,
            )

        # Success — issue a signed session cookie.
        web_auth.record_login_attempt(ip, success=True)
        sid = uuid.uuid4().hex
        cookie_value = web_auth.sign_session(sid)
        web_audit.login_success(ip, ua=ua)
        web_audit.session_create(sid, ip)

        # Form POST → 303 redirect to /. JSON POST → JSON response with
        # csrf_token (caller stashes it for subsequent fetch() requests).
        if is_form:
            resp = RedirectResponse(url="/", status_code=303)
        else:
            csrf = web_auth.make_csrf_token(sid)
            resp = JSONResponse({"ok": True, "csrf_token": csrf})
        resp.set_cookie(
            web_auth.cookie_name(),
            cookie_value,
            max_age=web_auth.session_ttl_seconds(),
            httponly=True,
            samesite="strict",
            # Secure flag set when the request looks TLS-terminated
            # (common reverse-proxy header). Localhost dev keeps it off.
            secure=request.headers.get("x-forwarded-proto") == "https",
        )
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        sid, err = _check_auth(request)
        ip = _client_ip(request)
        if sid and sid != "__localhost_no_auth__":
            web_audit.session_destroy(sid, ip, reason="logout")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(web_auth.cookie_name())
        return resp

    # ---------- v1.21: auth-gated routes ----------

    @app.get("/")
    async def index(request: Request):
        sid, err = _check_auth(request)
        if err:
            return RedirectResponse(url="/login", status_code=303)
        # v1.21: mint a CSRF token bound to this session and embed it
        # in the page. Frontend reads from <meta name="csrf-token"> and
        # sends as X-CSRF-Token on POSTs.
        csrf = web_auth.make_csrf_token(sid)
        return HTMLResponse(_index_page(csrf_token=csrf))

    @app.post("/chat")
    async def chat(request: Request, body: dict = Body(default={})):
        # v1.21: auth gate. The auth_sid (signed cookie) is independent
        # of the conversation-session_id below — auth gates access while
        # body.session_id keys conversation history.
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        ip = _client_ip(request)

        # v1.21: rate limit before doing real work.
        ok, retry_after = web_auth.rate_limit_take(auth_sid, "chat")
        if not ok:
            web_audit.rate_limited(auth_sid, ip, "chat", retry_after)
            return JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        # v1.21: CSRF check for state-changing POSTs.
        if not _check_csrf(request, auth_sid):
            web_audit.csrf_failure(auth_sid, ip, "/chat")
            return JSONResponse(
                {"error": "missing or invalid CSRF token"},
                status_code=403,
            )

        if not isinstance(body, dict):
            body = {}
        req = (body.get("request") or "").strip()
        sid = (body.get("session_id") or "").strip() or uuid.uuid4().hex
        if not req:
            return JSONResponse({"error": "empty request"})

        # Audit log the chat call (just length, not content).
        web_audit.chat(auth_sid, ip, len(req))

        # v1.3 pairing — only enforced when the operator opts in.
        if _pairing_required() and not gw.is_authorized(GATEWAY_NAME, sid):
            code = gw.request_pairing(GATEWAY_NAME, sid, user_label=sid[:8])
            return JSONResponse({
                "error": "pairing required",
                "pairing_code": code,
                "instructions":
                    f"ask the bot owner: janus pair approve {code}",
            })

        # v1.4: intercept /swarm slash commands at the gateway. Same
        # dispatch logic as cli_rich, telegram, whatsapp — text-only
        # response, no executor invocation.
        if req.startswith("/swarm"):
            from .. import swarms as _swarms
            arg = req[len("/swarm"):].strip()
            return JSONResponse({
                "session_id": sid,
                "output": _swarms.slash.handle(arg),
                "slash": True,
            })

        # v1.19.1 — /interview slash on web. Text-only response, no
        # executor invocation. Same subcommands as the CLI/Telegram
        # versions: enable drip with optional category filter, /pause,
        # /about-me. Drip questions get appended to subsequent normal
        # chat replies (post-turn hook).
        if req.startswith("/interview"):
            arg = req[len("/interview"):].strip()
            return JSONResponse({
                "session_id": sid,
                "output": _web_interview_handle(sid, arg),
                "slash": True,
            })

        # UserPromptSubmit hook can deny / rewrite.
        try:
            up = hooks.fire(hooks.USER_PROMPT_SUBMIT, {"request": req})
            if not up.allow:
                return JSONResponse({"error": f"blocked by hook: {up.reason}"})
            if up.modified_args and isinstance(up.modified_args.get("request"), str):
                req = up.modified_args["request"]
        except Exception:
            pass

        messages = _load_or_create_session(sid)

        # v1.3 self-introduction — first turn for a brand-new session
        # gets a soul-aware greeting prepended to the response.
        intro = ""
        if not messages:
            intro = gw.greeting() + "\n\n"

        # v1.19.1 — drip-mode pre-turn: if interview question pending,
        # treat user input as the answer (also passes to executor).
        drip_ack_message = ""
        try:
            from .. import interviews as _iv
            drip_handled, drip_ack = _iv.consume_pending_drip_answer(
                "web", sid, req,
            )
            if drip_handled and drip_ack:
                drip_ack_message = f"→ {drip_ack}\n\n"
        except Exception:
            pass

        mode = permissions.normalize(config.APPROVAL_MODE)
        base_approver = _make_web_approver(mode)
        caps = CapabilitySet()
        tools = default_registry(capabilities=caps)
        approver = make_protected(base_approver, caps, mode)
        preamble = memory.prepend_for_prompt()

        record: dict[str, Any] = {
            "ts": logger.now_iso(),
            "model": config.MODEL,
            "workspace": str(config.WORKSPACE),
            "request": req,
            "gateway": GATEWAY_NAME,
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
        # v1.3: persist messages + mode so they survive process restart.
        try:
            _persist_session(sid, messages, mode)
        except Exception:
            pass
        # v1.3 L3 #2 — per-chat cost ledger.
        try:
            ts = cost.turn_stats()
            cost.record_per_chat(
                gateway=GATEWAY_NAME, chat_id=sid,
                identity=gw.identity_for(GATEWAY_NAME, sid) or "",
                model=config.MODEL,
                prompt_tokens=ts.prompt_tokens,
                completion_tokens=ts.completion_tokens, usd=ts.usd,
            )
        except Exception:
            pass
        try:
            hooks.fire(hooks.STOP, {"request": req, "output": output})
        except Exception:
            pass

        # v1.19.1 — drip post-turn + inferred-suggestion offer appended
        # to the response body. Best-effort.
        drip_suffix = ""
        try:
            from .. import interviews as _iv, interview_inferred as _inf
            drip_q = _iv.get_drip_question("web", sid)
            if drip_q is not None:
                question_text, _fqid = drip_q
                drip_suffix += (
                    f"\n\n---\n\n💬 **Quick question:** {question_text}\n\n"
                    f"_(answer normally, 'skip' to skip, 'stop drip' to pause)_"
                )
            offer = _inf.pop_pending("web", sid)
            if offer is not None:
                drip_suffix += f"\n\n---\n\n💡 {_inf.render_offer(offer)}"
        except Exception:
            pass

        final_output = (
            (drip_ack_message + intro + output + drip_suffix)
            if output
            else (drip_ack_message + intro + drip_suffix or "(no output)")
        )
        return JSONResponse({"output": final_output, "session_id": sid})

    @app.post("/home")
    async def set_home(request: Request, body: dict = Body(default={})):
        """v1.3: designate this browser session as the web home channel.

        Cron output and cross-platform messages route here when this
        session is online.

        v1.21: auth + CSRF required.
        """
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        ip = _client_ip(request)
        ok, ra = web_auth.rate_limit_take(auth_sid, "read")
        if not ok:
            web_audit.rate_limited(auth_sid, ip, "read", ra)
            return JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            )
        if not _check_csrf(request, auth_sid):
            web_audit.csrf_failure(auth_sid, ip, "/home")
            return JSONResponse(
                {"error": "missing or invalid CSRF token"},
                status_code=403,
            )
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return JSONResponse({"error": "session_id required"})
        gw.set_home(GATEWAY_NAME, sid)
        web_audit.mutate(auth_sid, ip, "/home", ["session_id"])
        logger.write({
            "ts": logger.now_iso(), "type": "sethome",
            "gateway": GATEWAY_NAME, "session_id": sid,
        })
        return JSONResponse({"ok": True, "home": sid})

    @app.get("/cost")
    async def get_cost(request: Request, session_id: str = ""):
        """v1.3 L3 #2 — per-chat cost summary. v1.21: auth required."""
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        ip = _client_ip(request)
        ok, ra = web_auth.rate_limit_take(auth_sid, "read")
        if not ok:
            web_audit.rate_limited(auth_sid, ip, "read", ra)
            return JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            )
        if not session_id:
            return JSONResponse({
                "summary": "session_id required (per-chat ledger)",
            })
        identity = gw.identity_for(GATEWAY_NAME, session_id) or ""
        return JSONResponse({
            "session_id": session_id,
            "identity": identity,
            "summary": cost.render_per_chat(GATEWAY_NAME, session_id, identity),
        })

    @app.get("/memory")
    async def get_memory(request: Request, category: str = ""):
        """v1.3: list memory categories or fetch one. v1.21: auth required.

        Pre-v1.21 this was UNAUTHENTICATED — anyone on the network could
        read the user's full memory dump including identity, soul, and
        relationship cards. Hardened in v1.21.
        """
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        ip = _client_ip(request)
        ok, ra = web_auth.rate_limit_take(auth_sid, "read")
        if not ok:
            web_audit.rate_limited(auth_sid, ip, "read", ra)
            return JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            )
        if category:
            return JSONResponse({
                "category": category,
                "body": memory.read(category),
            })
        cats = memory.list_categories()
        return JSONResponse({
            "categories": cats,
            "configured": list(config.MEMORY_CATEGORIES),
            "all": {c: memory.read(c) for c in cats},
        })

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

    # v1.21: bootstrap-token visibility. Read or create the token; on
    # first start it's freshly generated, so print it. Subsequent starts
    # find it on disk and stay quiet (the user already saw it once or
    # has `cat ~/.janus/web_token`).
    token_path = config.HOME / "web_token"
    is_fresh = not token_path.exists()
    token = web_auth.get_or_create_bootstrap_token()

    print(f"janus web UI on http://{chosen_host}:{chosen_port}")
    print(f"login at http://{chosen_host}:{chosen_port}/login")

    is_local = chosen_host in ("127.0.0.1", "localhost", "::1")
    if is_fresh:
        print()
        print("  bootstrap token (paste at /login):")
        print(f"    {token}")
        print(f"  (saved to {token_path}, mode 0600)")
        print("  rotate with: janus web rotate-token")
        print()
    if not is_local:
        print(
            "  WARNING: binding non-localhost. Put a TLS-terminating "
            "reverse proxy (Caddy/nginx) in front of this — Janus "
            "speaks HTTP only by design.\n"
            "  Example Caddyfile:\n"
            "    janus.example.com {\n"
            f"        reverse_proxy {chosen_host}:{chosen_port}\n"
            "    }\n"
        )
    if _localhost_no_auth():
        print(
            "  WARNING: JANUS_WEB_LOCALHOST_NO_AUTH=1 — auth bypassed "
            "for localhost requests. Only set this if you trust every "
            "process on this machine."
        )

    app = _build_app()
    uvicorn.run(app, host=chosen_host, port=chosen_port, log_level="info")
    return 0


def rotate_token_cmd() -> int:
    """`janus web rotate-token` — generate a fresh bootstrap token.

    Existing signed sessions remain valid until expiry. Anyone holding
    the OLD token can no longer create new sessions.
    """
    config.ensure_home()
    new_token = web_auth.rotate_bootstrap_token()
    web_audit.token_rotate(ip="local-cli")
    print(f"new bootstrap token: {new_token}")
    print(f"  saved to {config.HOME / 'web_token'} (mode 0600)")
    print("  existing logged-in sessions remain valid until they expire")
    return 0
