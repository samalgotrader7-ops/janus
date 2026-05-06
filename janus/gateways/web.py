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
from pathlib import Path
from typing import Any

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
