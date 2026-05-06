// janus web UI v1.22 — shared frontend code.
//
// No framework dependency. Each panel registers itself in `panels`
// keyed by its hash route. Nav clicks update document.location.hash;
// hashchange handler swaps the active panel and calls its mount fn.
//
// Auth model: signed session cookie set at /login. CSRF token comes
// from <meta name="csrf-token"> on the index page; we attach it as
// X-CSRF-Token on every non-GET fetch.

(function () {
  'use strict';

  // ---------- shared utilities ----------

  const CSRF_TOKEN = (() => {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : '';
  })();

  function escapeHTML(s) {
    return String(s).replace(
      /[<>&"']/g,
      (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c])
    );
  }

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') e.className = v;
        else if (k === 'html') e.innerHTML = v;
        else if (k.startsWith('on') && typeof v === 'function') {
          e.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (v !== null && v !== undefined) {
          e.setAttribute(k, v);
        }
      }
    }
    for (const c of children) {
      if (c == null) continue;
      e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return e;
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign(
      { 'content-type': 'application/json' },
      opts.headers || {}
    );
    if (opts.method && opts.method.toUpperCase() !== 'GET') {
      headers['x-csrf-token'] = CSRF_TOKEN;
    }
    const r = await fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: 'same-origin',
    });
    if (r.status === 401) {
      window.location = '/login';
      throw new Error('redirected to login');
    }
    let data;
    try {
      data = await r.json();
    } catch (e) {
      data = { error: 'invalid response from server' };
    }
    return { status: r.status, data: data };
  }

  function setFooter(msg) {
    const f = document.getElementById('footer-msg');
    if (f) f.textContent = msg || '';
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ---------- panel registry ----------

  const panels = {};

  function registerPanel(name, opts) {
    panels[name] = opts;
  }

  function activatePanel(name) {
    if (!panels[name]) name = 'chat';
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
    document.querySelectorAll('nav.sidenav a').forEach((a) => a.classList.remove('active'));
    const target = document.getElementById('panel-' + name);
    const navLink = document.querySelector(`nav.sidenav a[data-panel="${name}"]`);
    if (target) target.classList.add('active');
    if (navLink) navLink.classList.add('active');
    if (panels[name].mount) {
      try {
        panels[name].mount();
      } catch (e) {
        setFooter('error mounting ' + name + ': ' + e.message);
      }
    }
  }

  function onHashChange() {
    const h = (window.location.hash || '').replace(/^#/, '') || 'chat';
    activatePanel(h);
  }

  window.addEventListener('hashchange', onHashChange);

  // ---------- chat panel ----------

  const chatState = {
    sessionId: (() => {
      let id = sessionStorage.getItem('janus_chat_session');
      if (!id) {
        id =
          (crypto.randomUUID && crypto.randomUUID()) ||
          Math.random().toString(36).slice(2);
        sessionStorage.setItem('janus_chat_session', id);
      }
      return id;
    })(),
    busy: false,
  };

  function chatAppend(role, body, isError) {
    const history = document.getElementById('chat-history');
    if (!history) return null;
    const turn = el(
      'div',
      { class: 'turn ' + role + (isError ? ' error' : '') },
      el('div', { class: 'who' }, role),
      el('div', { class: 'body' }, body || '')
    );
    history.appendChild(turn);
    history.scrollTop = history.scrollHeight;
    return turn;
  }

  async function chatSend() {
    if (chatState.busy) return;
    const ta = document.getElementById('chat-input');
    const send = document.getElementById('chat-send');
    const req = (ta.value || '').trim();
    if (!req) return;
    chatAppend('user', req);
    ta.value = '';
    chatState.busy = true;
    if (send) send.disabled = true;
    setFooter('thinking...');
    const pending = chatAppend('assistant', '...');
    try {
      const r = await api('/chat', {
        method: 'POST',
        body: { request: req, session_id: chatState.sessionId },
      });
      if (r.data && r.data.error) {
        pending.querySelector('.body').textContent = r.data.error;
        pending.classList.add('error');
      } else {
        pending.querySelector('.body').textContent =
          (r.data && r.data.output) || '(no output)';
      }
    } catch (e) {
      pending.querySelector('.body').textContent = 'request failed: ' + e.message;
      pending.classList.add('error');
    } finally {
      chatState.busy = false;
      if (send) send.disabled = false;
      setFooter('');
    }
  }

  registerPanel('chat', {
    mount() {
      const ta = document.getElementById('chat-input');
      if (ta) {
        ta.focus();
        ta.onkeydown = (e) => {
          // Ctrl+Enter or Cmd+Enter sends.
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            chatSend();
          }
        };
      }
      const send = document.getElementById('chat-send');
      if (send) send.onclick = chatSend;
    },
  });

  // ---------- memory panel ----------

  registerPanel('memory', {
    async mount() {
      const list = document.getElementById('memory-list');
      const filter = document.getElementById('memory-filter');
      const refresh = document.getElementById('memory-refresh');
      if (!list) return;
      const load = async () => {
        clear(list);
        list.appendChild(
          el('div', { class: 'item' }, el('div', { class: 'item-meta' }, 'loading...'))
        );
        const type = filter ? filter.value : '';
        const r = await api('/api/cards' + (type ? '?type=' + encodeURIComponent(type) : ''));
        clear(list);
        if (r.data.error) {
          list.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, r.data.error)));
          return;
        }
        const cards = r.data.cards || [];
        if (cards.length === 0) {
          list.appendChild(
            el(
              'div',
              { class: 'item' },
              el('div', { class: 'item-meta' }, 'no cards yet — try /interview to fill in your profile')
            )
          );
          return;
        }
        for (const c of cards) {
          const item = el(
            'div',
            { class: 'item' },
            el(
              'div',
              { class: 'item-title' },
              el('span', { class: 'tag ' + (c.type || '') }, c.type || ''),
              el('span', { class: 'tag scope' }, c.scope || ''),
              ' ',
              c.subject || '(no subject)'
            ),
            el(
              'div',
              { class: 'item-meta' },
              `conf ${(c.confidence || 0).toFixed(2)} · imp ${(c.importance || 0).toFixed(2)} · dur ${(c.durability || 0).toFixed(2)} · ${c.id || ''}`
            )
          );
          if (c.body) {
            item.appendChild(el('div', { class: 'item-body' }, c.body));
          }
          list.appendChild(item);
        }
        setFooter(`${cards.length} card(s) shown`);
      };
      if (filter) filter.onchange = load;
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- skills panel ----------

  registerPanel('skills', {
    async mount() {
      const list = document.getElementById('skills-list');
      const refresh = document.getElementById('skills-refresh');
      if (!list) return;
      const load = async () => {
        clear(list);
        list.appendChild(
          el('div', { class: 'item' }, el('div', { class: 'item-meta' }, 'loading...'))
        );
        const r = await api('/api/skills');
        clear(list);
        if (r.data.error) {
          list.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, r.data.error)));
          return;
        }
        const skills = r.data.skills || [];
        if (skills.length === 0) {
          list.appendChild(
            el(
              'div',
              { class: 'item' },
              el('div', { class: 'item-meta' }, 'no skills installed — try /skills install-bundled')
            )
          );
          return;
        }
        for (const s of skills) {
          const stateClass = s.state === 'promoted' ? 'promoted' : 'quarantined';
          list.appendChild(
            el(
              'div',
              { class: 'item' },
              el(
                'div',
                { class: 'item-title' },
                el('span', { class: 'tag ' + stateClass }, s.state || ''),
                ' ',
                s.name
              ),
              el(
                'div',
                { class: 'item-meta' },
                `${s.version || ''} · ${s.description || ''}`
              )
            )
          );
        }
        setFooter(`${skills.length} skill(s) listed`);
      };
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- files panel ----------

  const filesState = { currentDir: '.', currentFile: null };

  registerPanel('files', {
    async mount() {
      const tree = document.getElementById('files-tree');
      const viewer = document.getElementById('files-viewer');
      const breadcrumb = document.getElementById('files-breadcrumb');
      if (!tree || !viewer) return;
      const renderEmpty = () => {
        clear(viewer);
        viewer.appendChild(
          el(
            'div',
            { class: 'empty' },
            'select a file from the tree to view its contents (read-only in v1.22.0)'
          )
        );
      };
      const loadDir = async (path) => {
        clear(tree);
        tree.appendChild(el('div', { class: 'file-entry' }, 'loading...'));
        const r = await api('/api/files?path=' + encodeURIComponent(path || '.'));
        clear(tree);
        if (r.data.error) {
          tree.appendChild(el('div', { class: 'file-entry' }, r.data.error));
          return;
        }
        filesState.currentDir = r.data.path;
        if (breadcrumb) breadcrumb.textContent = r.data.path;
        // ".." entry if not at workspace root.
        if (r.data.path !== '.' && r.data.path !== r.data.workspace) {
          const up = el('div', { class: 'file-entry dir' }, '../');
          up.onclick = () => loadDir(r.data.parent || '.');
          tree.appendChild(up);
        }
        for (const e of r.data.entries || []) {
          const node = el(
            'div',
            { class: 'file-entry ' + (e.is_dir ? 'dir' : '') },
            e.name + (e.is_dir ? '/' : '')
          );
          node.onclick = e.is_dir
            ? () => loadDir(e.path)
            : () => loadFile(e.path, node);
          tree.appendChild(node);
        }
        setFooter(`${r.data.entries.length} entries in ${r.data.path}`);
      };
      const loadFile = async (path, node) => {
        document
          .querySelectorAll('#files-tree .file-entry.active')
          .forEach((n) => n.classList.remove('active'));
        if (node) node.classList.add('active');
        clear(viewer);
        viewer.appendChild(el('div', { class: 'empty' }, 'loading ' + path + '...'));
        const r = await api('/api/files/read?path=' + encodeURIComponent(path));
        clear(viewer);
        if (r.data.error) {
          viewer.appendChild(el('div', { class: 'empty' }, r.data.error));
          return;
        }
        filesState.currentFile = path;
        viewer.appendChild(
          el('div', { class: 'item-meta', style: 'margin-bottom:10px;' }, `${path}  ·  ${r.data.size} bytes`)
        );
        viewer.appendChild(el('pre', null, r.data.content || ''));
        setFooter('viewing ' + path);
      };
      renderEmpty();
      await loadDir('.');
    },
  });

  // ---------- bootstrap ----------

  function setupSignout() {
    const btn = document.getElementById('btn-signout');
    if (btn) {
      btn.onclick = async () => {
        await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
        window.location = '/login';
      };
    }
  }

  function setupNav() {
    document.querySelectorAll('nav.sidenav a').forEach((a) => {
      a.onclick = (e) => {
        const panel = a.getAttribute('data-panel');
        if (!panel) return;
        e.preventDefault();
        window.location.hash = panel;
      };
    });
  }

  window.addEventListener('DOMContentLoaded', () => {
    setupSignout();
    setupNav();
    onHashChange();
  });
})();
