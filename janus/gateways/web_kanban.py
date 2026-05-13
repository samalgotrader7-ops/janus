"""
gateways/web_kanban.py — web UI for the Kanban board (v1.42.1).

Routes registered into the main FastAPI app from `_build_app()`:

  GET  /kanban                — full HTML board page (six columns)
  GET  /kanban/api/list       — JSON of every task
  GET  /kanban/api/status     — dispatcher state + per-status counts
  POST /kanban/api/add        — create a task
  POST /kanban/api/transition — change a task's status
  POST /kanban/api/delete     — delete a task
  POST /kanban/api/dispatcher — start | stop the dispatcher

Auth + CSRF: every route uses the same gates as the rest of web.py
(`_check_auth`, `_check_csrf`). On state-changing POSTs we require
`X-CSRF-Token` matching the token embedded in the page.

The board is rendered server-side as an empty shell; the JS polls
`/kanban/api/list` every 3 seconds and re-renders the columns. Keeps
the page reactive without WebSockets.
"""

from __future__ import annotations

import html
import json
from typing import Any

# v1.42.2: FastAPI types are imported at module level so route
# annotations (`request: Request`) resolve through typing.get_type_hints
# in the same module the route is defined in. Closure-scope imports
# triggered FastAPI's 422 "field required" because it treated the
# `request` parameter as a query field. web_kanban.py is only imported
# from web.py which already gated FastAPI's availability — safe to
# do here unconditionally.
from fastapi import Body, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

from .. import branding
from ..kanban import dispatcher as _kd
from ..kanban import state as _state
from ..kanban import store as _store


# Order matters — left-to-right on the board.
_COLUMNS = [
    (_state.BACKLOG,     "Backlog"),
    (_state.READY,       "Ready"),
    (_state.IN_PROGRESS, "In Progress"),
    (_state.BLOCKED,     "Blocked"),
    (_state.COMPLETED,   "Completed"),
    (_state.FAILED,      "Failed"),
]


def _kanban_page(csrf_token: str = "") -> str:
    cols_html = "\n".join(
        f'      <section class="col" data-status="{s}">'
        f'<header><h2>{label}</h2>'
        f'<span class="count" data-count-for="{s}">0</span></header>'
        f'<div class="tasks" data-tasks-for="{s}"></div></section>'
        for s, label in _COLUMNS
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="csrf-token" content="{html.escape(csrf_token)}">
  <title>Janus Kanban</title>
  <style>
    :root {{ --brand: {branding.BRAND_COLOR}; --bg: #0f0e13; --fg: #e8e7eb;
             --muted: #8a8794; --card: #1a1922; --border: #2c2a36; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: var(--bg); color: var(--fg); }}
    header.bar {{ display:flex; align-items:center; gap:1rem; padding:.75rem 1rem;
                  border-bottom: 1px solid var(--border); }}
    header.bar h1 {{ margin:0; font-size:1.1rem; color: var(--brand); }}
    header.bar .dispatcher {{ font-size:.85rem; color: var(--muted); }}
    header.bar .dispatcher.running {{ color: #4ade80; }}
    header.bar button {{ background: var(--brand); color: #fff; border: none;
                          padding:.4rem .8rem; border-radius:4px; cursor:pointer;
                          font-size:.85rem; }}
    header.bar button.ghost {{ background: transparent; color: var(--fg);
                                border: 1px solid var(--border); }}
    main.board {{ display:grid; grid-template-columns: repeat(6, 1fr); gap:.5rem;
                  padding:.5rem; height: calc(100vh - 110px); overflow:hidden; }}
    section.col {{ background: var(--card); border: 1px solid var(--border);
                    border-radius:6px; display:flex; flex-direction:column;
                    min-height:0; }}
    section.col header {{ display:flex; justify-content:space-between;
                            padding:.5rem .75rem; border-bottom:1px solid var(--border); }}
    section.col header h2 {{ margin:0; font-size:.85rem; text-transform:uppercase;
                              letter-spacing:.05em; color: var(--muted); }}
    section.col .count {{ font-size:.75rem; color: var(--muted); }}
    section.col .tasks {{ flex:1; overflow-y:auto; padding:.5rem; }}
    .task {{ background: var(--bg); border:1px solid var(--border);
              border-radius:4px; padding:.5rem .6rem; margin-bottom:.4rem;
              font-size:.85rem; cursor:default; }}
    .task .id {{ color: var(--muted); font-family: ui-monospace, monospace;
                  font-size:.75rem; }}
    .task .title {{ margin:.2rem 0; }}
    .task .meta {{ font-size:.7rem; color: var(--muted);
                    display:flex; gap:.5rem; flex-wrap:wrap; }}
    .task .meta .agent::before {{ content:"@"; }}
    .task .meta .deps {{ color: #fbbf24; }}
    .task .actions {{ display:flex; gap:.3rem; margin-top:.3rem; }}
    .task .actions button {{ background:transparent; border:1px solid var(--border);
                              color: var(--fg); padding:.1rem .35rem;
                              border-radius:3px; font-size:.7rem; cursor:pointer; }}
    .task .actions button:hover {{ border-color: var(--brand); }}
    .task .err {{ color:#f87171; font-size:.7rem; margin-top:.2rem; }}
    form.add {{ display:flex; gap:.4rem; padding:.5rem 1rem;
                 background: var(--card); border-bottom:1px solid var(--border); }}
    form.add input, form.add select {{ background: var(--bg); color: var(--fg);
                                       border:1px solid var(--border);
                                       border-radius:3px; padding:.3rem .5rem;
                                       font-size:.85rem; }}
    form.add input[name="title"] {{ flex:1; }}
    form.add button {{ background: var(--brand); color:#fff; border:none;
                        padding:.3rem .8rem; border-radius:3px; cursor:pointer; }}
    .toast {{ position:fixed; bottom:1rem; right:1rem; background: var(--card);
              border:1px solid var(--border); padding:.5rem .8rem;
              border-radius:4px; font-size:.85rem; opacity:0;
              transition: opacity .2s; }}
    .toast.show {{ opacity:1; }}
    .toast.err {{ border-color:#f87171; color:#f87171; }}
  </style>
</head>
<body>
  <header class="bar">
    <h1>Janus Kanban</h1>
    <span class="dispatcher" id="dispatcher-status">dispatcher: loading…</span>
    <button id="btn-dispatcher" class="ghost">start</button>
    <a href="/" style="margin-left:auto;color:var(--muted);text-decoration:none;font-size:.85rem;">← chat</a>
  </header>
  <form class="add" id="add-form" autocomplete="off">
    <input name="title" placeholder="task title" required>
    <select name="agent_profile">
      <option value="developer">developer</option>
      <option value="researcher">researcher</option>
      <option value="coder">coder</option>
      <option value="documenter">documenter</option>
      <option value="reviewer">reviewer</option>
      <option value="tester">tester</option>
      <option value="claude">claude</option>
    </select>
    <input name="workspace" placeholder="workspace path (optional)">
    <input name="parent_ids" placeholder="parent ids (csv, optional)">
    <button type="submit">add</button>
  </form>
  <main class="board">
{cols_html}
  </main>
  <div id="toast" class="toast"></div>
  <script>
    const CSRF = document.querySelector('meta[name="csrf-token"]').content;
    const toast = document.getElementById('toast');
    function notify(msg, err=false) {{
      toast.textContent = msg;
      toast.className = 'toast show' + (err ? ' err' : '');
      setTimeout(() => toast.className = 'toast', 2500);
    }}
    function esc(s) {{ return String(s||'').replace(/[&<>\"]/g,c=>(
      {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}}[c])); }}

    async function api(path, opts={{}}) {{
      const headers = {{ 'X-CSRF-Token': CSRF }};
      if (opts.body) headers['Content-Type'] = 'application/json';
      const r = await fetch(path, {{ ...opts, headers }});
      const ct = r.headers.get('content-type') || '';
      const body = ct.includes('json') ? await r.json() : await r.text();
      if (!r.ok) throw new Error(body.error || body || r.statusText);
      return body;
    }}

    function renderTask(t) {{
      const deps = t.parents && t.parents.length
        ? `<span class="deps">deps: ${{t.parents.join(',')}}</span>` : '';
      const ws = t.workspace
        ? `<span title="${{esc(t.workspace)}}">📂 ${{esc(t.workspace.split('/').pop()||t.workspace)}}</span>` : '';
      const err = t.last_error
        ? `<div class="err">${{esc(t.last_error.slice(0,200))}}</div>` : '';
      const actions = [];
      if (t.status === 'ready' || t.status === 'backlog') {{
        actions.push(`<button data-act="block" data-id="${{t.id}}">block</button>`);
      }} else if (t.status === 'blocked') {{
        actions.push(`<button data-act="unblock" data-id="${{t.id}}">unblock</button>`);
      }} else if (t.status === 'failed') {{
        actions.push(`<button data-act="retry" data-id="${{t.id}}">retry</button>`);
      }}
      actions.push(`<button data-act="delete" data-id="${{t.id}}">×</button>`);
      return `<div class="task">
        <div class="id">#${{t.id}}</div>
        <div class="title">${{esc(t.title)}}</div>
        <div class="meta">
          <span class="agent">${{esc(t.agent)}}</span>
          ${{deps}}${{ws}}
        </div>
        ${{err}}
        <div class="actions">${{actions.join('')}}</div>
      </div>`;
    }}

    async function refresh() {{
      try {{
        const [tasks, status] = await Promise.all([
          api('/kanban/api/list'),
          api('/kanban/api/status'),
        ]);
        const cols = {{}};
        for (const t of tasks) (cols[t.status] = cols[t.status] || []).push(t);
        for (const sec of document.querySelectorAll('section.col')) {{
          const s = sec.dataset.status;
          const list = cols[s] || [];
          sec.querySelector('[data-count-for="'+s+'"]').textContent = list.length;
          sec.querySelector('[data-tasks-for="'+s+'"]').innerHTML =
            list.map(renderTask).join('') || '';
        }}
        const ds = document.getElementById('dispatcher-status');
        const btn = document.getElementById('btn-dispatcher');
        if (status.running) {{
          ds.textContent = 'dispatcher: running';
          ds.classList.add('running');
          btn.textContent = 'stop';
        }} else {{
          ds.textContent = 'dispatcher: stopped';
          ds.classList.remove('running');
          btn.textContent = 'start';
        }}
      }} catch(e) {{
        notify('refresh failed: '+e.message, true);
      }}
    }}

    document.getElementById('btn-dispatcher').onclick = async () => {{
      const action = document.getElementById('btn-dispatcher').textContent === 'start'
        ? 'start' : 'stop';
      try {{
        await api('/kanban/api/dispatcher', {{
          method:'POST', body: JSON.stringify({{action}}),
        }});
        await refresh();
      }} catch(e) {{ notify(e.message, true); }}
    }};

    document.getElementById('add-form').addEventListener('submit', async (ev) => {{
      ev.preventDefault();
      const fd = new FormData(ev.target);
      const parents = (fd.get('parent_ids')||'').toString().split(',')
        .map(s => parseInt(s.trim(),10)).filter(n => !isNaN(n));
      try {{
        await api('/kanban/api/add', {{
          method:'POST',
          body: JSON.stringify({{
            title: fd.get('title'),
            agent_profile: fd.get('agent_profile'),
            workspace: fd.get('workspace') || '',
            parent_ids: parents,
          }})
        }});
        ev.target.reset();
        notify('task added');
        refresh();
      }} catch(e) {{ notify(e.message, true); }}
    }});

    document.querySelector('main.board').addEventListener('click', async (ev) => {{
      const btn = ev.target.closest('button[data-act]');
      if (!btn) return;
      const id = parseInt(btn.dataset.id, 10);
      const act = btn.dataset.act;
      try {{
        if (act === 'delete') {{
          await api('/kanban/api/delete', {{
            method:'POST', body: JSON.stringify({{id}}),
          }});
        }} else {{
          await api('/kanban/api/transition', {{
            method:'POST', body: JSON.stringify({{id, action: act}}),
          }});
        }}
        refresh();
      }} catch(e) {{ notify(e.message, true); }}
    }});

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>"""


def register_kanban_routes(
    app,
    *,
    check_auth,
    check_csrf,
    web_auth,
) -> None:
    """Wire the kanban routes into the given FastAPI app.

    `check_auth(request)` and `check_csrf(request, sid)` are the same
    helpers web.py uses for its own routes — passed in to avoid the
    circular import they'd cause otherwise.
    `web_auth` is the module providing `make_csrf_token(sid)` for the
    page renderer.
    """
    @app.get("/kanban")
    async def kanban_page(request: Request):
        sid, err = check_auth(request)
        if err:
            return RedirectResponse(url="/login", status_code=303)
        csrf = web_auth.make_csrf_token(sid)
        return HTMLResponse(_kanban_page(csrf_token=csrf))

    @app.get("/kanban/api/list")
    async def kanban_list(request: Request):
        sid, err = check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        tasks = _store.list_tasks()
        return JSONResponse([
            {
                "id": t.id, "status": t.status, "agent": t.agent_profile,
                "title": t.title, "parents": t.parent_ids,
                "workspace": t.workspace, "description": t.description,
                "last_error": t.last_error,
                "retry_count": t.retry_count, "max_retries": t.max_retries,
                "created_at": t.created_at, "completed_at": t.completed_at,
            }
            for t in tasks
        ])

    @app.get("/kanban/api/status")
    async def kanban_status(request: Request):
        sid, err = check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        return JSONResponse({
            "running": _kd.is_running(),
            "counts": _store.counts_by_status(),
        })

    @app.post("/kanban/api/add")
    async def kanban_add(request: Request, body: dict = Body(default={})):
        sid, err = check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        if not check_csrf(request, sid):
            return JSONResponse({"error": "csrf failed"}, status_code=403)
        title = (body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title required"}, status_code=400)
        try:
            t = _store.create_task(
                title=title,
                agent_profile=(body.get("agent_profile") or "developer").strip(),
                description=(body.get("description") or "").strip(),
                prompt=(body.get("prompt") or "").strip(),
                workspace=(body.get("workspace") or "").strip(),
                parent_ids=list(body.get("parent_ids") or []),
                max_retries=int(body.get("max_retries") or 1),
            )
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"id": t.id, "status": t.status})

    @app.post("/kanban/api/transition")
    async def kanban_transition(request: Request, body: dict = Body(default={})):
        sid, err = check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        if not check_csrf(request, sid):
            return JSONResponse({"error": "csrf failed"}, status_code=403)
        try:
            tid = int(body["id"])
        except (KeyError, ValueError, TypeError):
            return JSONResponse({"error": "id required"}, status_code=400)
        action = (body.get("action") or "").lower()
        # Map user actions to legal target states.
        target_map = {
            "block":   _state.BLOCKED,
            "unblock": _state.BACKLOG,
            "retry":   _state.BACKLOG,
            "ready":   _state.READY,
            "done":    _state.COMPLETED,
            "fail":    _state.FAILED,
        }
        if action not in target_map:
            return JSONResponse(
                {"error": f"unknown action: {action}"}, status_code=400,
            )
        new_status = target_map[action]
        extra: dict[str, Any] = {}
        if action == "retry":
            # Manual retry — let store accept BACKLOG via the FAILED→BACKLOG
            # legal transition. Caller is the human.
            pass
        try:
            t = _store.set_status(tid, new_status, **extra)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"id": t.id, "status": t.status})

    @app.post("/kanban/api/delete")
    async def kanban_delete(request: Request, body: dict = Body(default={})):
        sid, err = check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        if not check_csrf(request, sid):
            return JSONResponse({"error": "csrf failed"}, status_code=403)
        try:
            tid = int(body["id"])
        except (KeyError, ValueError, TypeError):
            return JSONResponse({"error": "id required"}, status_code=400)
        ok = _store.delete_task(tid)
        return JSONResponse({"ok": ok})

    @app.post("/kanban/api/dispatcher")
    async def kanban_dispatcher(request: Request, body: dict = Body(default={})):
        sid, err = check_auth(request)
        if err:
            return JSONResponse({"error": err}, status_code=401)
        if not check_csrf(request, sid):
            return JSONResponse({"error": "csrf failed"}, status_code=403)
        action = (body.get("action") or "").lower()
        if action == "start":
            started = _kd.start()
            return JSONResponse({"running": True, "started": started})
        if action == "stop":
            was = _kd.stop()
            return JSONResponse({"running": False, "stopped": was})
        return JSONResponse(
            {"error": "action must be start|stop"}, status_code=400,
        )
