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
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .. import app as janus_app  # avoid name collision with FastAPI `app` local
from .. import config, executor, logger, memory, skills, hooks, permissions
from .. import branding, cost
from ..tools import default_registry, make_protected, CapabilitySet
from . import _common as gw
from . import web_auth, web_audit


# v1.22: static frontend files. Located alongside this module so they
# ship inside the wheel via package_data declaration in pyproject.toml.
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"


def _read_template(name: str) -> str:
    """Read a static template (HTML with __PLACEHOLDER__ tokens).

    Returns empty string if the file is missing — caller decides
    whether that's fatal or a fall-back path.
    """
    p = STATIC_DIR / name
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""

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

# v1.33.2 — process start timestamp for the /api/health endpoint's
# uptime_seconds field. Module import time is close enough to
# server-start time for monitoring purposes (delta is the FastAPI
# bootstrap, ~ms).
import time as _time
_PROCESS_START_TS = _time.time()


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


def _try_import_static_files():
    """v1.22: StaticFiles lives in fastapi.staticfiles. Separate import
    helper so legacy test stubs that mock _try_import_fastapi don't
    have to know about it."""
    try:
        from fastapi.staticfiles import StaticFiles
        return StaticFiles
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


def _make_web_approver(mode: str, auth_sid: str = "", loop=None):
    """v1.22.0a: mode-aware approver that bridges ASK decisions to the
    browser via SSE + modal.

    Pre-v1.22.0a, ASK decisions returned False (deny) because the page
    had no inline approval UI. v1.22.0a uses web_bridge.request_approval
    to block the worker thread until the user clicks approve/deny in a
    modal delivered via Server-Sent Events.

    `auth_sid` and `loop` are required for the bridge to work; older
    callers (no auth_sid) get the legacy ASK→DENY behavior so the
    function stays back-compat for callers that haven't migrated.
    """
    def approver(action_label: str, details: str, **kw) -> bool:
        # v1.31.5 — detect plan action FIRST via the shared
        # ``plan_render.is_plan_action`` helper. Field-validation
        # finding: ExitPlanMode is risk=read which
        # permissions.decide(read, *) ALLOWs in every mode, so
        # pre-v1.31.5 the web approver returned True silently and
        # the v1.30.0 plan-review modal never rendered. Same root
        # cause as the cli_rich + telegram fixes. Plan actions
        # always proceed to the bridge regardless of mode.
        try:
            from .. import plan_render as _plan_render
            is_plan = _plan_render.is_plan_action(action_label)
        except Exception:
            _plan_render = None  # type: ignore
            is_plan = bool(
                action_label
                and "exit_plan_mode" in action_label.lower()
            )
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )

        if not is_plan:
            decision = permissions.decide(risk, mode)
            if decision == permissions.ALLOW:
                return True
            if decision == permissions.DENY:
                return False

        # ASK — defer to the user via SSE modal. Falls through to deny
        # if we don't have the bridge wired (auth_sid/loop missing,
        # e.g., legacy callers that didn't pass them through).
        if not auth_sid or loop is None:
            return False
        from . import web_bridge
        # v1.30.0 — when this is an ExitPlanMode call, attach a parsed
        # plan payload so the web client can render the dedicated
        # plan-review modal (metric pills + step list + file chips)
        # instead of the generic approval prompt.
        plan_payload: dict | None = None
        if is_plan and _plan_render is not None:
            try:
                parsed = _plan_render.parse_plan(details)
                plan_payload = _plan_render.build_web_payload(
                    parsed, details, mode=mode,
                )
            except Exception:
                plan_payload = None
        return web_bridge.request_approval(
            auth_sid=auth_sid, loop=loop,
            label=action_label, details=details, risk=str(risk),
            plan=plan_payload,
        )
    return approver


def _make_web_clarify_callback(auth_sid: str, loop):
    """v1.22.0a: clarify callback for the web gateway.

    Returns a callable matching tools.clarify.Clarify's signature:
    (question: str, choices: list[str] | None) -> str. Blocks the
    worker thread until the user types or clicks an answer in the
    browser modal.
    """
    def callback(question: str, choices):
        if not auth_sid or loop is None:
            return "[clarify unavailable: web bridge not wired]"
        from . import web_bridge
        return web_bridge.request_clarify(
            auth_sid=auth_sid, loop=loop,
            question=question, choices=list(choices or []),
        )
    return callback


# v1.22.0: inline _LOGIN_HTML and _INDEX_HTML strings were removed.
# The frontend lives in janus/gateways/static/ as plain HTML/CSS/JS
# files served by FastAPI. Templates with __PLACEHOLDER__ tokens go
# through _read_template() + .replace() at request time.



def _index_page(csrf_token: str = "") -> str:
    mode = permissions.normalize(config.APPROVAL_MODE)
    template = _read_template("index.html")
    if not template:
        return "<h1>janus</h1><p>static/index.html missing — reinstall janus</p>"
    return (
        template
        .replace("__LOGO_SVG__", branding.svg_logo("currentColor"))
        .replace("__BRAND__", branding.BRAND_COLOR)
        .replace("__VERSION__", branding.VERSION)
        .replace("__TAGLINE__", html.escape(branding.TAGLINE))
        .replace("__MODEL__", html.escape(config.MODEL))
        .replace("__WORKSPACE__", html.escape(str(config.WORKSPACE)))
        .replace("__MODE__", html.escape(mode))
        .replace("__GREETING__", html.escape(gw.greeting()))
        .replace("__CSRF_TOKEN__", html.escape(csrf_token))
    )


def _login_page(error: str = "") -> str:
    err_block = ""
    if error:
        err_block = f'<div class="err">{html.escape(error)}</div>'
    template = _read_template("login.html")
    if not template:
        # Hard fallback — we lost the template but still need to let
        # the user log in. Bare-minimum form, no CSS.
        return (
            "<form method='post' action='/login'>"
            f"{err_block}"
            "<input type='password' name='token' placeholder='token' autofocus>"
            "<button type='submit'>sign in</button></form>"
        )
    return (
        template
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

    # v1.33.3 — token-bucket rate limiter for /login + /api/* (Phase 6.4).
    # Health endpoint is exempt; monitoring probes constantly. Real
    # client IP comes from X-Real-IP / X-Forwarded-For when behind
    # the reverse proxy from Phase 6.1.
    from .. import web_rate_limit as _rl

    @app.middleware("http")
    async def _rate_limit_mw(request, call_next):
        path = request.url.path
        if not _rl.is_rate_limited_path(path):
            return await call_next(request)
        limiter = _rl.get_default_limiter()
        # Build identity from headers + raw socket fallback.
        try:
            headers = dict(request.headers)
        except Exception:
            headers = {}
        client_host = (
            request.client.host if getattr(request, "client", None) else ""
        )
        key = _rl.client_ip_from_headers(headers, fallback=client_host) or "unknown"
        allowed, retry_after = limiter.consume(key)
        if not allowed:
            from fastapi.responses import JSONResponse as _Resp
            ra = max(1, int(retry_after) + 1)
            return _Resp(
                status_code=429,
                content={
                    "error": "rate limit exceeded",
                    "retry_after_seconds": ra,
                },
                headers={"Retry-After": str(ra)},
            )
        return await call_next(request)

    # v1.22: mount static assets at /static (CSS, JS, vendor libraries
    # later). The HTML templates with placeholders go through dedicated
    # routes that substitute __VERSION__ / __MODEL__ / __CSRF_TOKEN__
    # at request time. Static-only assets are served raw.
    StaticFiles = _try_import_static_files()
    if StaticFiles is not None and STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR), html=False),
            name="static",
        )

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
        # v1.22.0a: pass auth_sid + running loop into the approver so it
        # can bridge ASK decisions to a browser modal via SSE.
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        base_approver = _make_web_approver(mode, auth_sid=auth_sid, loop=loop)
        caps = CapabilitySet()
        tools = default_registry(capabilities=caps)
        # v1.22.0a: replace the bundled callback-less Clarify with one
        # bound to this auth session so clarify(question) prompts the
        # user via the SSE modal instead of returning [clarify unavailable].
        try:
            from ..tools.clarify import Clarify as _Clarify
            tools.remove_tool("clarify")
            tools.add_tool(_Clarify(
                callback=_make_web_clarify_callback(auth_sid, loop),
            ))
        except Exception:
            pass
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
            # v1.22.0a: run the (synchronous) chat turn in a thread so
            # its blocking approver wait doesn't freeze the FastAPI
            # event loop. The bridge schedules SSE notifications onto
            # the loop via run_coroutine_threadsafe.
            #
            # v1.25.0 Phase 0: route through app.run_turn so events
            # flow through the surface-agnostic substrate. Full async
            # iteration over app.chat_events (replacing to_thread
            # entirely) is a follow-up cleanup — Phase 0 only needs
            # the substrate plumbing.
            def _run_executor():
                return janus_app.run_turn(
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
            output, trace = await _asyncio.to_thread(_run_executor)
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

        # v1.24.1: broadcast memory.changed so the memory panel refreshes.
        # Chat turns can create new cards (propose_diff → apply_cards).
        # Best-effort — never break the chat flow.
        try:
            from . import web_bridge as _wb
            _wb._broadcast_from_thread(
                loop, auth_sid,
                {"type": "memory.changed", "reason": "chat_turn"},
            )
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

        # v1.29.3: skill auto-offer parity (extends v1.28.1 cli_rich-only).
        # Same gates as cli_rich + telegram: top pattern only,
        # AUTO_OFFER_MIN_OCCURRENCES threshold, mark_offered triggers
        # cooldown. Best-effort wrap.
        try:
            from .. import skill_proposer as _sp
            patterns = _sp.list_offerable(current_trace=trace)
            if patterns:
                top = patterns[0]
                if top.occurrences >= _sp.AUTO_OFFER_MIN_OCCURRENCES:
                    drip_suffix += (
                        f"\n\n---\n\n🪄 {top.description}.\n\n"
                        f"`/skills propose {top.id}` to draft, "
                        f"`/skills decline {top.id}` to silence."
                    )
                    _sp.mark_offered(top.id)
        except Exception:
            pass

        final_output = (
            (drip_ack_message + intro + output + drip_suffix)
            if output
            else (drip_ack_message + intro + drip_suffix or "(no output)")
        )

        # v1.37.2 — Phase 10.1.2: /goal Ralph Loop post-turn hook.
        # State tracking + response-shape hints only — auto-continue
        # execution arrives in v1.37.3 (frontend will poll
        # /api/goal/status and re-POST when next_prompt is set).
        # The 'goal' field shape is stable: callers can ignore it
        # safely if they don't know about it.
        goal_payload: dict[str, Any] = {}
        try:
            from .. import goal_loop as _gl
            _scope = f"web:{sid}"
            _decision = _gl.after_turn(_scope, output or "")
            if _decision.achieved:
                goal_payload = {
                    "status": "achieved",
                    "reason": _decision.reason,
                }
            elif _decision.paused:
                _marker = "cycle" if _decision.cycle_detected else (
                    "budget" if _decision.budget_exhausted else "paused"
                )
                goal_payload = {
                    "status": "paused",
                    "reason": _decision.reason,
                    "marker": _marker,
                }
            elif _decision.next_prompt:
                goal_payload = {
                    "status": "continue",
                    "next_prompt": _decision.next_prompt,
                    "reason": _decision.reason,
                }
            # v1.37.4 — surface budget alert + cumulative cost so
            # the frontend can render the gauge / warning banner.
            if goal_payload:
                if _decision.budget_alert is not None:
                    goal_payload["budget_alert"] = _decision.budget_alert
                if _decision.cost_usd > 0:
                    goal_payload["cost_usd"] = round(_decision.cost_usd, 6)
        except Exception:
            pass

        resp_body: dict[str, Any] = {
            "output": final_output,
            "session_id": sid,
        }
        if goal_payload:
            resp_body["goal"] = goal_payload
        return JSONResponse(resp_body)

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

    # ---------- v1.22.0: API endpoints for the SPA panels ----------

    def _gate_get(request):
        """Common GET-route gate: returns (auth_sid, error_response_or_None).

        v1.22 panels consume these — auth + rate-limit only. CSRF is
        skipped because GETs don't change state.
        """
        auth_sid, err = _check_auth(request)
        if err:
            return None, JSONResponse({"error": err}, status_code=401)
        ip = _client_ip(request)
        ok, ra = web_auth.rate_limit_take(auth_sid, "read")
        if not ok:
            web_audit.rate_limited(auth_sid, ip, "read", ra)
            return None, JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            )
        return auth_sid, None

    @app.get("/api/cards")
    async def api_cards(
        request: Request,
        type: str = "",
        scope: str = "",
        limit: int = 200,
    ):
        """v1.22: list v1.18 typed memory cards with optional filters.

        Filters are applied in pure Python; no DB query change. Returns
        each card with metadata + a body preview (first 400 chars).
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from .. import memory_index, memory_cards
            try:
                memory_index.reconcile()
            except Exception:
                pass
            rows = memory_index.list_all() or []
            cards: list[dict] = []
            for r in rows:
                if type and r.get("type") != type:
                    continue
                if scope and r.get("scope") != scope:
                    continue
                body_preview = ""
                try:
                    card = memory_cards.read_card(Path(r["path"]))
                    body_preview = (card.content or "")[:400]
                except Exception:
                    pass
                cards.append({
                    "id": r.get("id", ""),
                    "type": r.get("type", ""),
                    "subject": r.get("subject", ""),
                    "scope": r.get("scope", ""),
                    "confidence": r.get("confidence", 0.0),
                    "importance": r.get("importance", 0.0),
                    "durability": r.get("durability", 0.0),
                    "body": body_preview,
                })
                if len(cards) >= limit:
                    break
            return JSONResponse({"cards": cards, "total": len(cards)})
        except Exception as e:
            return JSONResponse({"error": f"cards listing failed: {e}"})

    @app.get("/api/skills")
    async def api_skills(request: Request):
        """v1.22: list installed skills with state + version + description."""
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            installed = skills.list_skills() or []
            out = []
            for s in installed:
                # list_skills returns dicts with keys we surface 1:1.
                # Be defensive about shape since skill metadata varies.
                out.append({
                    "name": s.get("name", "") if isinstance(s, dict) else getattr(s, "name", ""),
                    "version": s.get("version", "") if isinstance(s, dict) else getattr(s, "version", ""),
                    "description": (
                        s.get("description", "") if isinstance(s, dict)
                        else getattr(s, "description", "")
                    )[:200],
                    "state": s.get("state", "") if isinstance(s, dict) else getattr(s, "state", ""),
                })
            return JSONResponse({"skills": out, "total": len(out)})
        except Exception as e:
            return JSONResponse({"error": f"skills listing failed: {e}"})

    @app.get("/api/files")
    async def api_files(request: Request, path: str = "."):
        """v1.22: workspace file tree listing.

        `path` is relative to config.WORKSPACE; resolved via
        security.resolve_within so callers can't escape the workspace.
        Returns entries sorted (dirs first, then files alpha).
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from .. import security
            ws = Path(config.WORKSPACE).resolve()
            target = security.resolve_within(ws, path)
            if not target.is_dir():
                return JSONResponse(
                    {"error": "not a directory"}, status_code=400,
                )
            entries = []
            for child in sorted(
                target.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            ):
                # Hide dotfiles by default — mirrors the CLI ergonomic.
                if child.name.startswith("."):
                    continue
                rel = str(child.relative_to(ws)).replace("\\", "/")
                entries.append({
                    "name": child.name,
                    "path": rel,
                    "is_dir": child.is_dir(),
                    "size": child.stat().st_size if child.is_file() else 0,
                })
            current_rel = (
                str(target.relative_to(ws)).replace("\\", "/")
                if target != ws else "."
            )
            parent_rel = ""
            if target != ws:
                parent_rel = str(target.parent.relative_to(ws)).replace("\\", "/") or "."
            return JSONResponse({
                "path": current_rel,
                "parent": parent_rel,
                "workspace": str(ws),
                "entries": entries,
            })
        except ValueError as e:
            # security.resolve_within raises ValueError on escape.
            return JSONResponse(
                {"error": f"path outside workspace: {e}"}, status_code=400,
            )
        except FileNotFoundError:
            return JSONResponse(
                {"error": "path not found"}, status_code=404,
            )
        except Exception as e:
            return JSONResponse({"error": f"listing failed: {e}"})

    @app.get("/api/files/read")
    async def api_files_read(request: Request, path: str = ""):
        """v1.22: read a workspace file. Returns content as text (UTF-8).

        Refuses files >1MB for now — Monaco editor + huge file handling
        comes in v1.22.0a along with edits. Binary files are detected
        and refused (don't dump bytes into a JSON string).
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        if not path:
            return JSONResponse({"error": "path required"}, status_code=400)
        try:
            from .. import security
            ws = Path(config.WORKSPACE).resolve()
            target = security.resolve_within(ws, path)
            if not target.is_file():
                return JSONResponse(
                    {"error": "not a file"}, status_code=400,
                )
            size = target.stat().st_size
            if size > 1_000_000:
                return JSONResponse(
                    {"error": f"file too large ({size} bytes); v1.22.0 caps at 1MB"},
                    status_code=413,
                )
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return JSONResponse(
                    {"error": "binary file (not UTF-8)"}, status_code=415,
                )
            return JSONResponse({
                "path": str(target.relative_to(ws)).replace("\\", "/"),
                "size": size,
                "content": content,
            })
        except ValueError as e:
            # security.resolve_within raises ValueError on escape.
            return JSONResponse(
                {"error": f"path outside workspace: {e}"}, status_code=400,
            )
        except FileNotFoundError:
            return JSONResponse(
                {"error": "file not found"}, status_code=404,
            )
        except Exception as e:
            return JSONResponse({"error": f"read failed: {e}"})

    # ---------- v1.24.0: file write (Files panel editing) ----------

    @app.post("/api/files/write")
    async def api_files_write(request: Request):
        """v1.24.0: write a file in the workspace.

        Body: {"path": str, "content": str}
        - path is relative to config.WORKSPACE; resolved via
          security.resolve_within (no escape).
        - content size capped at 1MB (matches read cap).
        - directories must already exist (no implicit mkdir — caller
          must use shell tool / chat for that to keep file ops
          straightforward).
        - existing files are overwritten atomically (temp + rename).
        """
        auth_sid, err_resp, ip = _gate_post(request, "/api/files/write")
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "json body required"}, status_code=400)
        path = (body.get("path") or "").strip()
        content = body.get("content")
        if not path:
            return JSONResponse({"error": "path required"}, status_code=400)
        if not isinstance(content, str):
            return JSONResponse(
                {"error": "content must be a string"}, status_code=400,
            )
        if len(content) > 1_000_000:
            return JSONResponse(
                {"error": f"content too large ({len(content)} bytes); 1MB cap"},
                status_code=413,
            )
        try:
            from .. import security
            ws = Path(config.WORKSPACE).resolve()
            target = security.resolve_within(ws, path)
            if not target.parent.is_dir():
                return JSONResponse(
                    {"error": f"parent directory missing: {target.parent}"},
                    status_code=400,
                )
            # Atomic write — temp file in same dir, then rename.
            tmp = target.with_suffix(target.suffix + ".tmp.janusweb")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
            web_audit.mutate(
                auth_sid, ip, "/api/files/write",
                [str(target.relative_to(ws)).replace("\\", "/")],
            )
            return JSONResponse({
                "ok": True,
                "path": str(target.relative_to(ws)).replace("\\", "/"),
                "size": len(content.encode("utf-8")),
            })
        except ValueError as e:
            return JSONResponse(
                {"error": f"path outside workspace: {e}"}, status_code=400,
            )
        except Exception as e:
            return JSONResponse({"error": f"write failed: {e}"})

    # ---------- v1.22.1: mutations (cards, skills, interview) ----------

    def _gate_post(request, route: str):
        """Common POST gate: auth + rate-limit + CSRF."""
        auth_sid, err = _check_auth(request)
        if err:
            return None, JSONResponse({"error": err}, status_code=401), None
        ip = _client_ip(request)
        ok, ra = web_auth.rate_limit_take(auth_sid, "read")
        if not ok:
            web_audit.rate_limited(auth_sid, ip, "read", ra)
            return None, JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            ), None
        if not _check_csrf(request, auth_sid):
            web_audit.csrf_failure(auth_sid, ip, route)
            return None, JSONResponse(
                {"error": "missing or invalid CSRF token"}, status_code=403,
            ), None
        return auth_sid, None, ip

    @app.post("/api/cards/{card_id}/delete")
    async def api_card_delete(card_id: str, request: Request):
        """v1.22.1: supersede (soft-delete) a memory card.

        The card moves to ~/.janus/memory/_superseded/ and stays there
        for MEMORY_PRUNE_SUPERSEDED_DAYS before final unlink. P5 holds —
        nothing is shredded; the user can recover by `mv` if needed.
        """
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/cards/{card_id}/delete",
        )
        if err_resp is not None:
            return err_resp
        try:
            from .. import memory_cards, memory_index
            moved = memory_cards.supersede(card_id)
            if moved is None:
                return JSONResponse(
                    {"error": "card not found"}, status_code=404,
                )
            try:
                memory_index.reconcile()
            except Exception:
                pass
            web_audit.mutate(auth_sid, ip, "/api/cards/delete", [card_id])
            # v1.24.1: notify SSE subscribers so the memory panel
            # can refresh without a manual click.
            try:
                from . import web_bridge as _wb
                import asyncio as _asyncio
                _wb._broadcast_from_thread(
                    _asyncio.get_running_loop(),
                    auth_sid,
                    {"type": "memory.changed", "reason": "card_deleted",
                     "card_id": card_id},
                )
            except Exception:
                pass
            return JSONResponse({"ok": True, "moved_to": str(moved)})
        except Exception as e:
            return JSONResponse({"error": f"delete failed: {e}"})

    @app.post("/api/skills/{name}/promote")
    async def api_skill_promote(name: str, request: Request):
        """v1.22.1: change a skill's state (quarantined / promoted / disabled).

        Body: {"state": "promoted"} (or any valid state). Wraps
        skills.promote(name, state).
        """
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/skills/{name}/promote",
        )
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        new_state = (
            (body.get("state") or "promoted") if isinstance(body, dict) else "promoted"
        )
        try:
            updated = skills.promote(name, new_state)
            web_audit.mutate(
                auth_sid, ip, f"/api/skills/{name}/promote", [new_state],
            )
            return JSONResponse({
                "ok": True,
                "name": getattr(updated, "name", name),
                "state": getattr(updated, "state", new_state),
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"promote failed: {e}"}, status_code=400,
            )

    # ---------- v1.31.0: skill_proposer suggestions panel ----------

    @app.get("/api/skills/suggestions")
    async def api_skill_suggestions(request: Request):
        """v1.31.0: pure-compute pattern detection — list offerable
        recurring patterns (after cooldown / accepted filter).

        Returns {"patterns": [{"id", "kind", "occurrences",
        "description"}, ...]}. No LLM call here — only pattern
        detection over the local trace + log.jsonl.

        Calling this endpoint marks the surfaced patterns as ``offered``
        (same shape as cli_rich's /skills suggestions: cooldown timer
        starts when the user actually sees the pattern). The mark is
        capped at the top 10 to keep parity with the cli_rich output.
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from .. import skill_proposer
            patterns = skill_proposer.list_offerable() or []
            out = []
            for p in patterns[:10]:
                out.append({
                    "id": p.id,
                    "kind": p.kind,
                    "occurrences": int(p.occurrences),
                    "description": p.description,
                })
                # Mark as offered so the cooldown timer respects the UX.
                try:
                    skill_proposer.mark_offered(p.id)
                except Exception:
                    pass
            return JSONResponse({
                "patterns": out,
                "total": len(patterns),
                "cooldown_days": int(
                    getattr(skill_proposer, "COOLDOWN_DAYS", 7)
                ),
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"suggestions failed: {e}"}, status_code=500,
            )

    @app.post("/api/skills/suggestions/{pattern_id}/propose")
    async def api_skill_propose(pattern_id: str, request: Request):
        """v1.31.0: LLM-draft a skill from a detected pattern.

        Mutating endpoint — same gating as /api/skills/promote.
        Returns {"ok", "name", "path", "state"} on success or
        {"error", ...} on failure. Failure to find the pattern id
        returns 404 (rather than 200 with error key) so the client
        can distinguish.
        """
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/skills/suggestions/{pattern_id}/propose",
        )
        if err_resp is not None:
            return err_resp
        try:
            from .. import skill_proposer
            patterns = skill_proposer.detect()
            target = next(
                (p for p in patterns if p.id == pattern_id), None,
            )
            if target is None:
                return JSONResponse(
                    {"error": f"no pattern with id {pattern_id}"},
                    status_code=404,
                )
            path = skill_proposer.draft_skill(target)
            web_audit.mutate(
                auth_sid, ip,
                f"/api/skills/suggestions/{pattern_id}/propose",
                [target.kind],
            )
            return JSONResponse({
                "ok": True,
                "name": path.stem,
                "path": str(path),
                "state": "quarantined",
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"propose failed: {e}"}, status_code=500,
            )

    @app.post("/api/skills/suggestions/{pattern_id}/decline")
    async def api_skill_decline(pattern_id: str, request: Request):
        """v1.31.0: silence a pattern offer for the cooldown window.

        Pure state-write — no LLM. Mutating endpoint though, so we
        gate it the same way for consistency + audit trail.
        """
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/skills/suggestions/{pattern_id}/decline",
        )
        if err_resp is not None:
            return err_resp
        try:
            from .. import skill_proposer
            skill_proposer.mark_declined(pattern_id)
            web_audit.mutate(
                auth_sid, ip,
                f"/api/skills/suggestions/{pattern_id}/decline",
                [],
            )
            return JSONResponse({
                "ok": True,
                "cooldown_days": int(
                    getattr(skill_proposer, "COOLDOWN_DAYS", 7)
                ),
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"decline failed: {e}"}, status_code=500,
            )

    @app.post("/api/skills/install-bundled")
    async def api_skills_install_bundled(request: Request):
        """v1.22.1: install bundled skill catalog into ~/.janus/skills/.

        Body: {"force": bool} optional. Returns counts.
        """
        auth_sid, err_resp, ip = _gate_post(
            request, "/api/skills/install-bundled",
        )
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        force = bool(body.get("force")) if isinstance(body, dict) else False
        try:
            from .. import skill_catalog
            result = skill_catalog.install_bundled(force=force)
            web_audit.mutate(
                auth_sid, ip, "/api/skills/install-bundled",
                ["force"] if force else [],
            )
            return JSONResponse({"ok": True, "result": result})
        except Exception as e:
            return JSONResponse({"error": f"install failed: {e}"})

    # ---------- v1.22.1: interview panel API ----------

    @app.get("/api/interview/state")
    async def api_interview_state(request: Request, session_id: str = ""):
        """v1.22.1: per-(gateway, browser-session) interview state.

        `session_id` is the conversation session id (from the chat
        panel). Defaults to "default" when not supplied.
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        sid = session_id or "default"
        try:
            from .. import interviews as _iv
            _iv.maybe_install_bundled()
            state = _iv.load_state("web", sid)
            library = _iv.load_all()
            completion = _iv.compute_completion(state, library)
            return JSONResponse({
                "session_id": sid,
                "mode": state.mode,
                "started_at": state.started_at,
                "current_category": state.current_category,
                "current_question_id": state.current_question_id,
                "drip_filter_category": state.drip_filter_category,
                "drip_quota_remaining": state.drip_quota_remaining,
                "drip_quota_resets_at": state.drip_quota_resets_at,
                "answered_count": len(state.answered),
                "skipped_count": len(state.skipped),
                "completion": completion,
                "categories": list(_iv.SUPPORTED_CATEGORIES),
            })
        except Exception as e:
            return JSONResponse({"error": f"interview state failed: {e}"})

    @app.post("/api/interview/start")
    async def api_interview_start(request: Request):
        """v1.22.1: start drip mode (optionally restricted to one category).

        Body: {"session_id": str, "category": str|"", "daily_count": int}
        Wraps the same logic as /interview slash command.
        """
        auth_sid, err_resp, ip = _gate_post(request, "/api/interview/start")
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = (body.get("session_id") or "default") if isinstance(body, dict) else "default"
        category = (body.get("category") or "").strip().lower() if isinstance(body, dict) else ""
        try:
            daily = int(body.get("daily_count") or 0) if isinstance(body, dict) else 0
        except (ValueError, TypeError):
            daily = 0
        try:
            from .. import interviews as _iv
            if category and category not in _iv.SUPPORTED_CATEGORIES:
                return JSONResponse(
                    {"error": f"unknown category: {category}"},
                    status_code=400,
                )
            arg = ""
            if daily > 0 and category:
                arg = category  # filter by category at default per_day
            elif daily > 0:
                arg = f"daily {daily}"
            elif category:
                arg = category
            output = _web_interview_handle(sid, arg)
            web_audit.mutate(
                auth_sid, ip, "/api/interview/start",
                [k for k in ("category", "daily_count") if body.get(k)],
            )
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"error": f"interview start failed: {e}"})

    @app.post("/api/interview/pause")
    async def api_interview_pause(request: Request):
        auth_sid, err_resp, ip = _gate_post(request, "/api/interview/pause")
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = (body.get("session_id") or "default") if isinstance(body, dict) else "default"
        try:
            output = _web_interview_handle(sid, "pause")
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"error": f"pause failed: {e}"})

    @app.get("/api/interview/about-me")
    async def api_interview_about_me(request: Request):
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            text = _web_render_about_me()
            return JSONResponse({"ok": True, "body": text})
        except Exception as e:
            return JSONResponse({"error": f"about-me failed: {e}"})

    # ---------- v1.22.2: agents / swarms / triggers panels ----------

    def _api_allow():
        """Approver factory for API tool invocations.

        The HTTP request itself is the approval — auth + CSRF + the
        user's explicit click in the panel UI. Tools called via the
        API run with always-allow.
        """
        def approver(*a, **kw):
            return True
        return approver

    @app.get("/api/agents")
    async def api_agents(request: Request):
        """v1.22.2: list scheduled agents (skill+trigger pairs)."""
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from ..tools.agent import AgentList
            tool = AgentList()
            raw = tool.run({}, _api_allow())
            return JSONResponse({"output": raw})
        except Exception as e:
            return JSONResponse({"error": f"agents list failed: {e}"})

    @app.post("/api/agents/{name}/run-now")
    async def api_agent_run_now(name: str, request: Request):
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/agents/{name}/run-now",
        )
        if err_resp is not None:
            return err_resp
        try:
            from ..tools.agent import AgentRunNow
            tool = AgentRunNow()
            output = tool.run({"name": name}, _api_allow())
            web_audit.mutate(auth_sid, ip, "/api/agents/run-now", [name])
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"error": f"run-now failed: {e}"})

    @app.post("/api/agents/{name}/set-enabled")
    async def api_agent_set_enabled(name: str, request: Request):
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/agents/{name}/set-enabled",
        )
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled")) if isinstance(body, dict) else True
        try:
            from ..tools.agent import AgentSetEnabled
            tool = AgentSetEnabled()
            output = tool.run(
                {"name": name, "enabled": enabled}, _api_allow(),
            )
            web_audit.mutate(
                auth_sid, ip, "/api/agents/set-enabled",
                [name, "enabled" if enabled else "disabled"],
            )
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"error": f"set-enabled failed: {e}"})

    @app.post("/api/agents/{name}/delete")
    async def api_agent_delete(name: str, request: Request):
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/agents/{name}/delete",
        )
        if err_resp is not None:
            return err_resp
        try:
            from ..tools.agent import AgentDelete
            tool = AgentDelete()
            output = tool.run({"name": name}, _api_allow())
            web_audit.mutate(auth_sid, ip, "/api/agents/delete", [name])
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"error": f"delete failed: {e}"})

    @app.get("/api/swarms/specs")
    async def api_swarm_specs(request: Request):
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from ..swarms import spec as _spec
            specs = _spec.list_specs() or []
            out = []
            for s in specs:
                out.append({
                    "name": getattr(s, "name", ""),
                    "description": (getattr(s, "description", "") or "")[:200],
                    "phases": len(getattr(s, "phases", []) or []),
                    "max_subagents": getattr(s, "max_subagents", None),
                    "max_budget_usd": getattr(s, "max_budget_usd", None),
                })
            return JSONResponse({"specs": out})
        except Exception as e:
            return JSONResponse({"error": f"specs list failed: {e}"})

    @app.get("/api/swarms/runs")
    async def api_swarm_runs(request: Request, limit: int = 50):
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from ..swarms import state as _swstate
            run_ids = _swstate.list_runs() or []
            # Newest first if list_runs returns sorted; otherwise rely on it.
            run_ids = run_ids[-limit:][::-1]
            return JSONResponse({"runs": run_ids})
        except Exception as e:
            return JSONResponse({"error": f"runs list failed: {e}"})

    @app.get("/api/triggers")
    async def api_triggers(request: Request):
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from ..triggers import base as _tb
            tlist = _tb.list_triggers() or []
            out = []
            for t in tlist:
                out.append({
                    "name": getattr(t, "name", ""),
                    "kind": getattr(t, "kind", ""),
                    "when": getattr(t, "when", ""),
                    "skill": getattr(t, "skill", ""),
                    "enabled": getattr(t, "enabled", True),
                    "deliver_to": getattr(t, "deliver_to", ""),
                })
            return JSONResponse({"triggers": out})
        except Exception as e:
            return JSONResponse({"error": f"triggers list failed: {e}"})

    # v1.35.5 — Phase 8.4: triggers CRUD (enable/disable/delete).
    # Run-now deferred (needs the daemon's event-loop context).

    def _trigger_path(name: str):
        """Resolve a trigger YAML path safely (no path traversal)."""
        import re as _re
        if not _re.match(r"^[a-zA-Z0-9_-]+$", name or ""):
            return None
        return config.TRIGGERS_DIR / f"{name}.yaml"

    def _trigger_set_enabled(name: str, enabled: bool):
        path = _trigger_path(name)
        if path is None or not path.exists():
            return False, "trigger not found"
        try:
            text = path.read_text(encoding="utf-8")
            import re as _re
            new_val = "true" if enabled else "false"
            # Replace `enabled: <anything>` line; if missing, append.
            pattern = _re.compile(r"^(enabled\s*:\s*)\S+\s*$", _re.MULTILINE)
            if pattern.search(text):
                text = pattern.sub(rf"\1{new_val}", text)
            else:
                if not text.endswith("\n"):
                    text += "\n"
                text += f"enabled: {new_val}\n"
            path.write_text(text, encoding="utf-8")
            return True, ""
        except Exception as e:
            return False, str(e)

    @app.post("/api/triggers/{name}/enable")
    async def api_trigger_enable(name: str, request: Request):
        auth_sid, err, _ip = _gate_post(request, f"/api/triggers/{name}")
        if err is not None:
            return err
        ok, msg = _trigger_set_enabled(name, True)
        if not ok:
            return JSONResponse(status_code=404 if "not found" in msg else 400,
                                 content={"error": msg})
        try:
            from .. import audit_log
            audit_log.record("trigger.enable", name=name)
        except Exception:
            pass
        return JSONResponse({"status": "ok", "enabled": True})

    @app.post("/api/triggers/{name}/disable")
    async def api_trigger_disable(name: str, request: Request):
        auth_sid, err, _ip = _gate_post(request, f"/api/triggers/{name}")
        if err is not None:
            return err
        ok, msg = _trigger_set_enabled(name, False)
        if not ok:
            return JSONResponse(status_code=404 if "not found" in msg else 400,
                                 content={"error": msg})
        try:
            from .. import audit_log
            audit_log.record("trigger.disable", name=name)
        except Exception:
            pass
        return JSONResponse({"status": "ok", "enabled": False})

    @app.post("/api/triggers/{name}/delete")
    async def api_trigger_delete(name: str, request: Request):
        auth_sid, err, _ip = _gate_post(request, f"/api/triggers/{name}")
        if err is not None:
            return err
        path = _trigger_path(name)
        if path is None or not path.exists():
            return JSONResponse(status_code=404, content={"error": "trigger not found"})
        try:
            path.unlink()
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"delete failed: {e}"})
        try:
            from .. import audit_log
            audit_log.record("trigger.delete", name=name)
        except Exception:
            pass
        return JSONResponse({"status": "ok", "deleted": name})

    # ---------- v1.29.1 — MCP catalog browser ----------

    # ---------- v1.34.0 — Generic incoming webhooks (Phase 7.5) ----------

    @app.post("/api/webhook/{key}")
    async def api_webhook(key: str, request: Request):
        """v1.34.0 — incoming webhook endpoint. Validates HMAC of
        raw body using the per-key shared secret in
        ~/.janus/webhooks.json, then renders the configured prompt
        template with payload substitutions and audits the fire.

        Returns:
          202 Accepted    when fired successfully
          401 Unauthorized when signature mismatch / missing
          404 Not Found    when key isn't configured

        NOT auth-gated through the cookie/token system — auth is
        the HMAC. Anyone with the secret can fire.
        """
        from .. import webhooks
        body = await request.body()
        sig = (
            request.headers.get("x-janus-signature")
            or request.headers.get("X-Janus-Signature")
        )
        result = webhooks.evaluate(key, body, sig)
        # Audit every webhook attempt (including failures) so abuse
        # patterns are visible in `janus audit --action webhook.`.
        try:
            from .. import audit_log
            audit_log.record(
                f"webhook.{result.status}",
                key=key,
                bytes=len(body),
            )
        except Exception:
            pass
        if result.status == "unknown_key":
            return JSONResponse(
                status_code=404,
                content={"error": result.detail},
            )
        if result.status == "bad_signature":
            return JSONResponse(
                status_code=401,
                content={"error": result.detail},
            )
        # Fired: return 202 + the rendered prompt so the caller can
        # log it / verify what was sent. The agent fire itself is
        # async / out-of-band — v1.34.0 ships the wire without
        # running the turn (turn-fire is a follow-up; needs care
        # around session ownership + rate limits).
        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "prompt_preview": (result.rendered_prompt or "")[:500],
            },
        )

    # ---------- v1.33.2 — Health endpoint (Phase 6.3) ----------

    @app.get("/api/health")
    async def api_health(request: Request):
        """Health endpoint for monitoring (Uptime Kuma / Better Stack /
        etc.). NOT auth-gated — health endpoints are conventionally
        public so monitoring can probe without credentials.

        Response: 200 with JSON:
          {
            "version": "1.33.2",
            "uptime_seconds": 1234.56,
            "last_turn_age_seconds": 42.0,    // null if no turns yet
            "services_active": ["janus-web", ...],  // [] if systemctl unavailable
            "status": "healthy" | "degraded"
          }

        Always 200 — clients use the `status` field to decide. This
        keeps the endpoint compatible with simple uptime monitors that
        treat any 2xx as "up". Operators can extend the check to
        require status=='healthy' if they want."""
        import time
        import json as _json
        try:
            from .. import branding as _br
            version = _br.VERSION
        except Exception:
            version = "unknown"

        uptime = max(0.0, time.time() - _PROCESS_START_TS)

        last_turn_age: float | None = None
        try:
            log_path = config.HOME / "log.jsonl"
            if log_path.exists() and log_path.stat().st_size > 0:
                # Tail-based: stat mtime tracks the last write to the
                # log, which is the last logged event (turn or tool).
                last_turn_age = max(
                    0.0, time.time() - log_path.stat().st_mtime,
                )
        except Exception:
            last_turn_age = None

        services_active: list[str] = []
        try:
            import shutil
            import subprocess
            if shutil.which("systemctl"):
                for svc in ("janus-web", "janus-telegram", "janus-daemon"):
                    r = subprocess.run(
                        ["systemctl", "--user", "is-active", f"{svc}.service"],
                        capture_output=True, text=True, timeout=2,
                    )
                    if (r.stdout or "").strip() == "active":
                        services_active.append(svc)
        except Exception:
            services_active = []

        # Healthy if web is alive at all (we're answering this
        # request) AND last turn within last 24h (or no turns yet).
        # Degraded otherwise — ops can decide whether to alert.
        status = "healthy"
        if last_turn_age is not None and last_turn_age > 86400:
            status = "degraded"

        return JSONResponse({
            "version": version,
            "uptime_seconds": round(uptime, 3),
            "last_turn_age_seconds": (
                round(last_turn_age, 3) if last_turn_age is not None else None
            ),
            "services_active": services_active,
            "status": status,
        })

    @app.get("/api/mcp/catalog")
    async def api_mcp_catalog(request: Request):
        """v1.29.1: list configured + connected MCP servers with
        per-server tool inventory. Connected servers contribute a
        live tool list; configured-only servers report
        ``connected: false`` and an empty tool list (the user can
        connect via the existing CLI to inspect tools).

        v1.31.14 — response now includes ``skipped`` array listing
        config entries that were dropped (HTTP-transport servers,
        malformed configs). Each entry has {name, reason, source}.
        Frontend renders these so users can see WHY their MCP isn't
        appearing instead of staring at silence."""
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from ..mcp import client as _mcp
            servers, skipped = _mcp.load_servers_with_diagnostics()
            active = _mcp.get_active_clients()
            out: list[dict] = []
            seen: set[str] = set()
            for name, cfg in servers.items():
                seen.add(name)
                entry = {
                    "name": name,
                    "command": cfg.command,
                    "args": list(cfg.args),
                    "enabled": cfg.enabled,
                    "connected": name in active,
                    "tools": [],
                }
                if name in active:
                    try:
                        tools = active[name].list_tools() or []
                    except Exception as e:
                        entry["error"] = (
                            f"list_tools: {type(e).__name__}: {e}"
                        )
                        tools = []
                    for tdef in tools:
                        params = (
                            (tdef.get("inputSchema") or {})
                            .get("properties") or {}
                        )
                        entry["tools"].append({
                            "name": tdef.get("name", ""),
                            "description": (
                                tdef.get("description") or ""
                            ).strip(),
                            "param_count": len(params),
                            "janus_name": (
                                f"mcp_{name}_{tdef.get('name', '')}"
                                .replace("-", "_")
                            ),
                        })
                out.append(entry)
            # Connected-not-configured servers (rare — only happens if
            # someone connects without writing a config file)
            for name, c in active.items():
                if name in seen:
                    continue
                entry = {
                    "name": name, "command": "(not in config)",
                    "args": [], "enabled": True,
                    "connected": True, "tools": [],
                }
                try:
                    tools = c.list_tools() or []
                except Exception:
                    tools = []
                for tdef in tools:
                    params = (
                        (tdef.get("inputSchema") or {})
                        .get("properties") or {}
                    )
                    entry["tools"].append({
                        "name": tdef.get("name", ""),
                        "description": (
                            tdef.get("description") or ""
                        ).strip(),
                        "param_count": len(params),
                        "janus_name": (
                            f"mcp_{name}_{tdef.get('name', '')}"
                            .replace("-", "_")
                        ),
                    })
                out.append(entry)
            # v1.31.14 — skipped entries surface alongside the live
            # ones so the frontend can render them.
            skipped_out = [
                {"name": s.name, "reason": s.reason, "source": s.source}
                for s in skipped
            ]
            return JSONResponse({"servers": out, "skipped": skipped_out})
        except Exception as e:
            return JSONResponse({"error": f"mcp catalog failed: {e}"})

    @app.get("/api/mcp/inspect")
    async def api_mcp_inspect(request: Request):
        """v1.29.1: full inputSchema for one MCP tool. Query params:
        ``server`` and ``tool``. 404-ish (200 with error key) if the
        server isn't connected."""
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        server = (request.query_params.get("server") or "").strip()
        tool_name = (request.query_params.get("tool") or "").strip()
        if not server or not tool_name:
            return JSONResponse({"error": "missing server / tool"})
        try:
            from ..mcp import client as _mcp
            active = _mcp.get_active_clients()
            client = active.get(server)
            if client is None:
                return JSONResponse({
                    "error": (
                        f"server '{server}' not connected — "
                        f"use `/mcp connect {server}` first"
                    ),
                })
            tools = client.list_tools() or []
            target = next(
                (t for t in tools if t.get("name") == tool_name), None,
            )
            if target is None:
                return JSONResponse({
                    "error": f"no tool '{tool_name}' on '{server}'",
                    "available": [
                        t.get("name", "") for t in tools
                    ],
                })
            return JSONResponse({
                "server": server,
                "tool": tool_name,
                "janus_name": (
                    f"mcp_{server}_{tool_name}".replace("-", "_")
                ),
                "description": (
                    target.get("description") or ""
                ).strip(),
                "input_schema": (
                    target.get("inputSchema")
                    or {"type": "object", "properties": {}}
                ),
            })
        except Exception as e:
            return JSONResponse({"error": f"mcp inspect failed: {e}"})

    # ---------- v1.22.3: shells / logs / cost / settings ----------

    @app.get("/api/shells")
    async def api_shells(request: Request):
        """v1.22.3: list background shells via shell_bg state."""
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from ..tools.shell_bg import ShellList
            tool = ShellList()
            raw = tool.run({}, _api_allow())
            return JSONResponse({"output": raw})
        except Exception as e:
            return JSONResponse({"error": f"shells list failed: {e}"})

    @app.post("/api/shells/run")
    async def api_shells_run(request: Request):
        auth_sid, err_resp, ip = _gate_post(request, "/api/shells/run")
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        cmd = (body.get("command") or "").strip() if isinstance(body, dict) else ""
        pty_mode = bool(body.get("pty")) if isinstance(body, dict) else False
        if not cmd:
            return JSONResponse(
                {"error": "command required"}, status_code=400,
            )
        # v1.24.1: PTY mode for interactive shells (POSIX only).
        if pty_mode:
            from ..tools import shell_pty as _spty
            if not _spty.is_supported():
                return JSONResponse(
                    {"error": (
                        "PTY shells require POSIX (pty module). Windows "
                        "ConPTY support lands in v1.24.2. Re-issue "
                        "without pty=true to use the captured-output mode."
                    )},
                    status_code=400,
                )
            try:
                shell_id = _spty.start_pty_shell(cmd)
                web_audit.mutate(
                    auth_sid, ip, "/api/shells/run",
                    ["command", "pty"],
                )
                return JSONResponse({
                    "ok": True,
                    "output": f"started PTY shell {shell_id}\n"
                              f"  cmd: {cmd}\n"
                              f"  attach to /api/shells/{shell_id}/stream\n",
                    "shell_id": shell_id,
                    "pty": True,
                })
            except Exception as e:
                return JSONResponse({"error": f"pty start failed: {e}"})
        try:
            from ..tools.shell_bg import ShellRunBg
            tool = ShellRunBg()
            output = tool.run({"command": cmd}, _api_allow())
            web_audit.mutate(auth_sid, ip, "/api/shells/run", ["command"])
            return JSONResponse({"ok": True, "output": output, "pty": False})
        except Exception as e:
            return JSONResponse({"error": f"shell run failed: {e}"})

    @app.post("/api/shells/{shell_id}/stdin")
    async def api_shell_stdin(shell_id: str, request: Request):
        """v1.24.1: write to a PTY shell's stdin. Body: {"data": str}.

        Only works for shells started with pty=true. For non-PTY shells
        returns 400 — there's no stdin to write to.
        """
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/shells/{shell_id}/stdin",
        )
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        data = (body.get("data") or "") if isinstance(body, dict) else ""
        if not isinstance(data, str):
            return JSONResponse(
                {"error": "data must be a string"}, status_code=400,
            )
        try:
            from ..tools import shell_pty as _spty
            if not _spty.is_supported():
                return JSONResponse(
                    {"error": "PTY stdin requires POSIX"},
                    status_code=400,
                )
            n = _spty.write_stdin(shell_id, data)
            web_audit.mutate(
                auth_sid, ip, f"/api/shells/{shell_id}/stdin",
                [f"{n}b"],
            )
            return JSONResponse({"ok": True, "bytes": n})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": f"stdin write failed: {e}"})

    @app.get("/api/shells/{shell_id}/output")
    async def api_shell_output(shell_id: str, request: Request):
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from ..tools.shell_bg import ShellOutput
            tool = ShellOutput()
            output = tool.run({"shell_id": shell_id}, _api_allow())
            return JSONResponse({"output": output})
        except Exception as e:
            return JSONResponse({"error": f"output read failed: {e}"})

    @app.get("/api/shells/{shell_id}/stream")
    async def api_shell_stream(shell_id: str, request: Request):
        """v1.24.0: SSE-stream the bg shell's stdout/stderr.

        The endpoint tails ~/.janus/shells/<id>/stdout.log + stderr.log
        every 200ms and yields any new bytes as SSE 'data' events. The
        browser's xterm.js writes the raw bytes to its terminal — full
        ANSI / cursor / color support.

        Stops naturally when:
          * the shell exits (status file reads 'exited' / 'killed' /
            'failed') AND no new bytes have appeared for 1 second
          * the client disconnects
          * `disconnected` reached via request.is_disconnected
        """
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        from fastapi.responses import StreamingResponse
        from ..tools import shell_bg as _sh
        import asyncio as _asyncio

        d = _sh._shell_dir(shell_id)
        if not d.is_dir():
            return JSONResponse(
                {"error": "no such shell"}, status_code=404,
            )
        stdout_path = d / "stdout.log"
        stderr_path = d / "stderr.log"

        async def gen():
            stdout_pos = 0
            stderr_pos = 0
            stable_count = 0  # consecutive ticks with no new bytes after exit
            while True:
                if await request.is_disconnected():
                    return
                # Refresh status (may transition pid-running -> exited).
                try:
                    status = _sh._refresh_status(shell_id)
                except Exception:
                    status = "unknown"

                # Read whatever is new in stdout/stderr.
                new_chunks: list[tuple[str, str]] = []
                for path, pos_attr in (
                    (stdout_path, "stdout"),
                    (stderr_path, "stderr"),
                ):
                    if not path.is_file():
                        continue
                    try:
                        with path.open("rb") as f:
                            f.seek(stdout_pos if pos_attr == "stdout" else stderr_pos)
                            data = f.read()
                    except OSError:
                        continue
                    if data:
                        if pos_attr == "stdout":
                            stdout_pos += len(data)
                        else:
                            stderr_pos += len(data)
                        # Decode liberally — bg shells may emit non-UTF-8.
                        text = data.decode("utf-8", errors="replace")
                        new_chunks.append((pos_attr, text))

                if new_chunks:
                    stable_count = 0
                    for stream_name, text in new_chunks:
                        # SSE encoding: each line of `text` becomes a
                        # data: line. xterm.js handles \r\n itself.
                        # We yield one event per chunk to preserve the
                        # cursor positioning.
                        import json as _json
                        payload = _json.dumps({
                            "stream": stream_name, "text": text,
                        })
                        yield f"event: chunk\ndata: {payload}\n\n"
                else:
                    # No new data. If shell has exited and stayed idle
                    # for a couple of ticks, send a final marker and
                    # close the stream.
                    if status in ("exited", "killed", "failed"):
                        stable_count += 1
                        if stable_count >= 5:  # ~1s of stability
                            import json as _json
                            yield (
                                "event: end\n"
                                f"data: {_json.dumps({'status': status})}\n\n"
                            )
                            return
                    # Heartbeat to keep the SSE connection alive.
                    if stable_count > 0 and stable_count % 25 == 0:
                        yield ":heartbeat\n\n"
                await _asyncio.sleep(0.2)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
        )

    @app.post("/api/shells/{shell_id}/kill")
    async def api_shell_kill(shell_id: str, request: Request):
        auth_sid, err_resp, ip = _gate_post(
            request, f"/api/shells/{shell_id}/kill",
        )
        if err_resp is not None:
            return err_resp
        try:
            from ..tools.shell_bg import ShellKill
            tool = ShellKill()
            output = tool.run({"shell_id": shell_id}, _api_allow())
            web_audit.mutate(auth_sid, ip, "/api/shells/kill", [shell_id])
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"error": f"kill failed: {e}"})

    @app.get("/api/logs")
    async def api_logs(
        request: Request,
        limit: int = 100,
        since: str | None = None,
        until: str | None = None,
        mode: str | None = None,
        model: str | None = None,
        q: str | None = None,
    ):
        """v1.22.3 / v1.34.7: tail of ~/.janus/log.jsonl with filters.

        Query params:
          limit:  max entries to return (default 100, capped at 1000)
          since:  ISO timestamp, return only entries with ts >= since
          until:  ISO timestamp, return only entries with ts <= until
          mode:   filter by permission mode (default/acceptEdits/...)
          model:  substring match on model id
          q:      substring match on the request field (case-insensitive)

        Filters are AND-combined. Returns most-recent first.
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            limit = max(1, min(int(limit or 100), 1000))
            entries: list[dict] = []
            log_path = config.LOG_FILE
            if log_path.is_file():
                try:
                    lines = log_path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                # Apply filters BEFORE limit so a tight filter doesn't
                # only see the last 100 raw lines.
                import json as _json
                q_lower = (q or "").lower() if q else None
                model_lower = (model or "").lower() if model else None
                for raw in lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = _json.loads(raw)
                    except Exception:
                        # Keep raw entries when no filter is active so
                        # users see corrupted lines instead of silent
                        # gaps.
                        if not (since or until or mode or model or q):
                            entries.append({"raw": raw})
                        continue
                    if not isinstance(rec, dict):
                        continue
                    ts = str(rec.get("ts", ""))
                    if since and ts < since:
                        continue
                    if until and ts > until:
                        continue
                    if mode and str(rec.get("mode", "")) != mode:
                        continue
                    if model_lower:
                        rec_model = str(rec.get("model", "")).lower()
                        if model_lower not in rec_model:
                            continue
                    if q_lower:
                        rec_req = str(rec.get("request", "")).lower()
                        if q_lower not in rec_req:
                            continue
                    entries.append(rec)
            # Most recent first, capped at limit.
            return JSONResponse({"entries": entries[-limit:][::-1]})
        except Exception as e:
            return JSONResponse({"error": f"logs read failed: {e}"})

    @app.get("/api/logs/stream")
    async def api_logs_stream(request: Request):
        """v1.24.1: SSE stream of new entries appended to log.jsonl.

        On connect, replays the last 20 entries so the client has
        context. Then tails the file every 500ms; new lines arrive as
        `entry` events.
        """
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        from fastapi.responses import StreamingResponse
        import asyncio as _asyncio
        import json as _json

        log_path = config.LOG_FILE

        async def gen():
            # Bootstrap: replay last 20 entries.
            try:
                if log_path.is_file():
                    lines = log_path.read_text(encoding="utf-8").splitlines()
                    pos = log_path.stat().st_size
                    for raw in lines[-20:]:
                        if not raw.strip():
                            continue
                        yield (
                            "event: entry\n"
                            f"data: {raw}\n\n"
                        )
                else:
                    pos = 0
            except OSError:
                pos = 0

            heartbeat_count = 0
            while True:
                if await request.is_disconnected():
                    return
                try:
                    if not log_path.is_file():
                        await _asyncio.sleep(1.0)
                        continue
                    size = log_path.stat().st_size
                    if size < pos:
                        # Log was rotated/truncated. Re-anchor.
                        pos = 0
                    if size == pos:
                        heartbeat_count += 1
                        if heartbeat_count >= 50:  # ~25s
                            yield ":heartbeat\n\n"
                            heartbeat_count = 0
                        await _asyncio.sleep(0.5)
                        continue
                    heartbeat_count = 0
                    with log_path.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                    pos = size
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        if not line.strip():
                            continue
                        yield (
                            "event: entry\n"
                            f"data: {line}\n\n"
                        )
                except Exception as e:
                    yield (
                        "event: error\n"
                        f"data: {_json.dumps({'msg': str(e)[:200]})}\n\n"
                    )
                    await _asyncio.sleep(1.0)
                await _asyncio.sleep(0.5)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
        )

    # ---------- v1.31.2: persistent grants panel ----------

    @app.get("/api/goal/status")
    async def api_goal_status(request: Request):
        """v1.37.2: read the active goal for this web session.

        Query string: ``?session_id=<sid>``. Returns the GoalState
        as JSON: text, status, turn_budget, turns_used, paused_at,
        plus a derived ``remaining`` field. ``{"goal": null}`` when
        no goal is set for the scope. Used by the frontend to render
        the goal banner + decide whether to auto-fire next_prompt
        (v1.37.3 will plumb that in).

        The scope key matches what the /chat endpoint writes:
        ``web:<session_id>``.
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        sid = (request.query_params.get("session_id") or "").strip()
        if not sid:
            return JSONResponse(
                {"error": "session_id required"}, status_code=400,
            )
        try:
            from .. import goals as _goals
            g = _goals.load(f"web:{sid}")
            if g is None:
                return JSONResponse({"goal": None})
            return JSONResponse({
                "goal": {
                    "text": g.text,
                    "status": g.status,
                    "turn_budget": g.turn_budget,
                    "turns_used": g.turns_used,
                    "remaining": g.remaining_turns(),
                    "created_at": g.created_at,
                    "updated_at": g.updated_at,
                    "paused_at": g.paused_at,
                    # v1.37.4: cost gauge fields
                    "cost_usd": round(g.cost_usd, 6),
                    "progress_ratio": round(g.progress_ratio(), 4),
                    "budget_alerts_fired": g.budget_alerts_fired,
                },
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"goal status failed: {e}"}, status_code=500,
            )

    @app.get("/api/grants")
    async def api_grants(request: Request):
        """v1.31.2: list persistent approval grants from
        ``~/.janus/approvals.json``.

        Returns ``{"grants": [{"tool", "risk"}, ...]}``. The web's
        ``ModeState`` lifecycle is per-HTTP-request so session
        grants don't survive between calls — only persistent grants
        are surfaced here. Same source of truth that cli_rich and
        telegram see (P5: plain-text persistent state).
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            from .. import permissions
            grants = permissions._load_persistent_grants()
            out = sorted(
                ({"tool": t, "risk": r} for (t, r) in grants),
                key=lambda d: (d["tool"], d["risk"]),
            )
            return JSONResponse({
                "grants": out,
                "total": len(out),
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"grants list failed: {e}"}, status_code=500,
            )

    @app.post("/api/grants/revoke")
    async def api_grants_revoke(request: Request):
        """v1.31.2: revoke a (tool, risk) persistent grant.

        Body: ``{"tool": str, "risk": str}``. The pair must exist
        in the persistent grants file; missing pair returns 404.
        """
        auth_sid, err_resp, ip = _gate_post(request, "/api/grants/revoke")
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be an object"}, status_code=400,
            )
        tool = str(body.get("tool") or "").strip()
        risk = str(body.get("risk") or "").strip()
        if not tool or not risk:
            return JSONResponse(
                {"error": "tool and risk required"}, status_code=400,
            )
        try:
            from .. import permissions
            grants = permissions._load_persistent_grants()
            key = (tool, risk)
            if key not in grants:
                return JSONResponse(
                    {"error": f"no persistent grant for {tool}/{risk}"},
                    status_code=404,
                )
            grants.discard(key)
            permissions._save_persistent_grants(grants)
            web_audit.mutate(
                auth_sid, ip, "/api/grants/revoke", [tool, risk],
            )
            return JSONResponse({
                "ok": True,
                "tool": tool,
                "risk": risk,
                "remaining": len(grants),
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"revoke failed: {e}"}, status_code=500,
            )

    @app.post("/api/grants/clear")
    async def api_grants_clear(request: Request):
        """v1.31.2: wipe ALL persistent grants. Drops the file
        contents (writes back an empty grants list)."""
        auth_sid, err_resp, ip = _gate_post(request, "/api/grants/clear")
        if err_resp is not None:
            return err_resp
        try:
            from .. import permissions
            removed = len(permissions._load_persistent_grants())
            permissions._save_persistent_grants(set())
            web_audit.mutate(
                auth_sid, ip, "/api/grants/clear", [str(removed)],
            )
            return JSONResponse({"ok": True, "removed": removed})
        except Exception as e:
            return JSONResponse(
                {"error": f"clear failed: {e}"}, status_code=500,
            )

    @app.get("/api/cost-detail")
    async def api_cost_detail(request: Request, days: int = 14):
        """v1.31.1: structured cost data for the web panel.

        Returns budget gauge, daily-rollup series, current turn,
        and the existing text summary in one payload — the panel
        renders the chart + gauge directly without the old
        text-only fallback.
        """
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        days = max(1, min(int(days or 14), 90))
        try:
            try:
                summary = cost.render_summary() or "(no usage yet)"
            except Exception:
                summary = ""
            ts = cost.turn_stats()
            try:
                budget = cost.budget_status()
            except Exception:
                budget = {
                    "budget": 0.0, "spent": 0.0, "remaining": 0.0,
                    "percent": 0.0, "configured": False,
                }
            try:
                daily = cost.daily_totals(since_days=days)
            except Exception:
                daily = []
            return JSONResponse({
                "summary": summary,
                "turn": {
                    "prompt_tokens": int(ts.prompt_tokens),
                    "completion_tokens": int(ts.completion_tokens),
                    "usd": float(ts.usd),
                },
                "budget": budget,
                "daily": daily,
                "days": days,
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"cost detail failed: {e}"}, status_code=500,
            )

    @app.get("/api/cost-summary")
    async def api_cost_summary(request: Request):
        """v1.22.3: aggregated cost summary. Reuses cost.render_summary."""
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            try:
                summary = cost.render_summary() or "(no usage yet)"
            except Exception:
                # Older builds may name it differently — fall back to
                # turn_stats so at least the user sees current model usage.
                ts = cost.turn_stats()
                summary = (
                    f"current turn:\n"
                    f"  prompt tokens: {ts.prompt_tokens}\n"
                    f"  completion tokens: {ts.completion_tokens}\n"
                    f"  cost: ${ts.usd:.4f}\n"
                )
            return JSONResponse({"summary": summary})
        except Exception as e:
            return JSONResponse({"error": f"cost summary failed: {e}"})

    # v1.35.6 — Phase 8.3: settings write. Whitelisted env keys can
    # be updated via POST; persisted to ~/.janus/.env. The runtime
    # environment is mutated for the current process so subsequent
    # turns see the new value (services like janus-web restart on
    # next pull anyway, but in-process updates are useful for
    # iterating without a restart).
    SETTINGS_WRITABLE_KEYS: frozenset = frozenset({
        "JANUS_APPROVAL",
        "JANUS_MODEL",
        "JANUS_API_BASE",
        "JANUS_TELEGRAM_VERBOSE",
        "JANUS_TOOL_SCHEMA_SLIM",
        "JANUS_TOKEN_BUDGET_COMPACT",
        "JANUS_INTERVIEW_CROSS_GATEWAY",
        "JANUS_BUDGET_USD",
        "JANUS_VERIFY_MODEL",
        "JANUS_SUBAGENT_MODEL",
        "JANUS_MEMORY_MODEL",
        "JANUS_TITLE_MODEL",
    })

    def _write_env_file(updates: dict[str, str]) -> tuple[bool, str]:
        """Add / replace keys in ~/.janus/.env. Preserves comments
        and unrelated lines. Idempotent."""
        env_path = config.HOME / ".env"
        try:
            existing_lines: list[str] = []
            if env_path.is_file():
                existing_lines = env_path.read_text(encoding="utf-8").splitlines()
            seen = set()
            out_lines: list[str] = []
            for line in existing_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    out_lines.append(line)
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    out_lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                else:
                    out_lines.append(line)
            for k, v in updates.items():
                if k not in seen:
                    out_lines.append(f"{k}={v}")
            text = "\n".join(out_lines).rstrip() + "\n"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(text, encoding="utf-8")
            try:
                env_path.chmod(0o600)
            except Exception:
                pass
            return True, ""
        except Exception as e:
            return False, str(e)

    @app.post("/api/settings")
    async def api_settings_write(request: Request):
        """v1.35.6: update whitelisted env keys + persist to
        ~/.janus/.env. Body: {key: <name>, value: <string>} or
        {updates: {key: value, ...}} for batch."""
        auth_sid, err_resp, _ip = _gate_post(request, "/api/settings")
        if err_resp is not None:
            return err_resp
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid JSON body"},
            )

        updates: dict[str, str] = {}
        if isinstance(body, dict):
            if "updates" in body and isinstance(body["updates"], dict):
                for k, v in body["updates"].items():
                    updates[str(k)] = str(v)
            elif "key" in body and "value" in body:
                updates[str(body["key"])] = str(body["value"])

        if not updates:
            return JSONResponse(
                status_code=400,
                content={"error": "missing 'key'+'value' or 'updates' dict"},
            )

        # Whitelist enforcement
        rejected = [k for k in updates if k not in SETTINGS_WRITABLE_KEYS]
        if rejected:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "key not writable via this endpoint",
                    "rejected": sorted(rejected),
                    "writable": sorted(SETTINGS_WRITABLE_KEYS),
                },
            )

        ok, msg = _write_env_file(updates)
        if not ok:
            return JSONResponse(
                status_code=500,
                content={"error": f"write failed: {msg}"},
            )

        # Update os.environ in-process so the running web sees the
        # new values without restart.
        for k, v in updates.items():
            os.environ[k] = v

        try:
            from .. import audit_log
            audit_log.record(
                "settings.write",
                keys=sorted(updates.keys()),
            )
        except Exception:
            pass

        return JSONResponse({
            "status": "ok",
            "updated_keys": sorted(updates.keys()),
        })

    @app.get("/api/settings")
    async def api_settings(request: Request):
        """v1.22.3: read-only view of mode + model + key env vars."""
        auth_sid, err_resp = _gate_get(request)
        if err_resp is not None:
            return err_resp
        try:
            mode = permissions.normalize(config.APPROVAL_MODE)
            return JSONResponse({
                "mode": mode,
                "model": config.MODEL,
                "api_base": config.API_BASE,
                "workspace": str(config.WORKSPACE),
                "home": str(config.HOME),
                "step_soft_cap": config.STEP_SOFT_CAP,
                "step_hard_cap": config.STEP_HARD_CAP,
                "step_progress_grace": config.STEP_PROGRESS_GRACE,
                "shell_timeout_max": config.SHELL_TIMEOUT_MAX,
                "session_ttl_seconds": web_auth.session_ttl_seconds(),
                "version": branding.VERSION,
            })
        except Exception as e:
            return JSONResponse({"error": f"settings read failed: {e}"})

    # ---------- v1.22.0a: SSE stream + approve/clarify endpoints ----------

    @app.get("/api/events")
    async def api_events(request: Request):
        """v1.22.0a: Server-Sent Events stream for approval / clarify
        modal delivery. Browser keeps this connection open; backend
        pushes events as worker threads request them.

        First payload is a `bootstrap` event that hydrates any modals
        already pending (e.g., user reconnected mid-flight after a
        page reload).
        """
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        from . import web_bridge
        from fastapi.responses import StreamingResponse
        import asyncio as _asyncio
        import json as _json

        queue = web_bridge.add_subscriber(auth_sid)

        async def gen():
            try:
                # Bootstrap: send any pending approvals/clarifies that
                # fired before this SSE connection opened.
                for entry in web_bridge.list_pending_approvals(auth_sid):
                    yield (
                        "event: approval_pending\n"
                        f"data: {_json.dumps(entry)}\n\n"
                    )
                for entry in web_bridge.list_pending_clarifies(auth_sid):
                    yield (
                        "event: clarify_pending\n"
                        f"data: {_json.dumps(entry)}\n\n"
                    )
                # Heartbeat every 25s keeps the connection alive
                # through proxies that drop idle connections.
                while True:
                    try:
                        evt = await _asyncio.wait_for(queue.get(), timeout=25.0)
                    except _asyncio.TimeoutError:
                        yield ":heartbeat\n\n"
                        continue
                    et = evt.get("type", "message")
                    yield f"event: {et}\ndata: {_json.dumps(evt)}\n\n"
            finally:
                web_bridge.remove_subscriber(auth_sid, queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",  # Nginx: don't buffer
            },
        )

    @app.post("/api/approve/{request_id}")
    async def api_approve(request_id: str, request: Request):
        """v1.22.0a: resolve a pending approval. Body: {"approve": bool}."""
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        ip = _client_ip(request)
        ok, ra = web_auth.rate_limit_take(auth_sid, "read")
        if not ok:
            return JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            )
        if not _check_csrf(request, auth_sid):
            web_audit.csrf_failure(auth_sid, ip, f"/api/approve/{request_id}")
            return JSONResponse(
                {"error": "missing or invalid CSRF token"}, status_code=403,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        decision = bool(body.get("approve")) if isinstance(body, dict) else False
        from . import web_bridge
        if not web_bridge.resolve_approval(request_id, decision):
            return JSONResponse(
                {"error": "no such approval (expired or already resolved)"},
                status_code=404,
            )
        web_audit.mutate(
            auth_sid, ip, f"/api/approve/{request_id}",
            ["approve" if decision else "deny"],
        )
        return JSONResponse({"ok": True, "decision": decision})

    @app.post("/api/clarify/{request_id}")
    async def api_clarify(request_id: str, request: Request):
        """v1.22.0a: resolve a pending clarify. Body: {"answer": str}."""
        auth_sid, err = _check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        ip = _client_ip(request)
        ok, ra = web_auth.rate_limit_take(auth_sid, "read")
        if not ok:
            return JSONResponse(
                {"error": "rate limited"}, status_code=429,
                headers={"Retry-After": str(int(ra) + 1)},
            )
        if not _check_csrf(request, auth_sid):
            web_audit.csrf_failure(auth_sid, ip, f"/api/clarify/{request_id}")
            return JSONResponse(
                {"error": "missing or invalid CSRF token"}, status_code=403,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        answer = (body.get("answer") if isinstance(body, dict) else "") or ""
        from . import web_bridge
        if not web_bridge.resolve_clarify(request_id, str(answer)):
            return JSONResponse(
                {"error": "no such clarify (expired or already resolved)"},
                status_code=404,
            )
        web_audit.mutate(auth_sid, ip, f"/api/clarify/{request_id}", ["answer"])
        return JSONResponse({"ok": True})

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

    # v1.31.15 — switch stdout to line-buffering so startup prints
    # appear immediately when the user runs ``nohup janus web > log
    # 2>&1`` (a redirected stdout block-buffers by default in
    # CPython, so the v1.31.14 version banner sat in the buffer
    # until ~4-8KB of activity flushed it). Field-validation: Sam
    # restarted with v1.31.14 and ``head -3 /tmp/janus-web.log``
    # showed only uvicorn's stderr-routed lines — the whole point
    # of the version banner was to make staleness visible at a
    # glance, but the buffer ate it. uvicorn's logging goes through
    # stderr (line-buffered by default + merged via 2>&1), which is
    # why its output appeared in head while ours didn't. Try/except
    # because reconfigure isn't available on every kind of stdout
    # (closed file, custom wrapper) — fall back gracefully.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # v1.21: bootstrap-token visibility. Read or create the token; on
    # first start it's freshly generated, so print it. Subsequent starts
    # find it on disk and stay quiet (the user already saw it once or
    # has `cat ~/.janus/web_token`).
    token_path = config.HOME / "web_token"
    is_fresh = not token_path.exists()
    token = web_auth.get_or_create_bootstrap_token()

    # v1.31.14 — version on the startup banner so future stale-process
    # bugs are visible at a glance. Field-validation finding from
    # Sam's VPS (2026-05-09): a janus web process started May 7 was
    # still running 2 days later, holding old code in memory while
    # /opt/janus on disk was at v1.31.13. All v1.29-v1.31 endpoints
    # returned 404. Without the version on startup, "is this process
    # stale?" required digging through pipx + ps + curl. With it,
    # `head -3 /var/log/janus-web.log` answers the question.
    # v1.31.15 — flush=True belt-and-suspenders so the version
    # banner reaches the log even on stdouts where reconfigure fails.
    print(
        f"janus web UI v{branding.VERSION} "
        f"on http://{chosen_host}:{chosen_port}",
        flush=True,
    )
    print(
        f"login at http://{chosen_host}:{chosen_port}/login",
        flush=True,
    )

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
