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

  // v1.31.0: skill_proposer suggestions UI. Renders inside the
  // skills panel above the installed-skills list. Mount fetches both
  // /api/skills/suggestions and /api/skills in parallel and renders
  // each section. Suggestions block hides when there are 0 patterns.
  async function loadSkillSuggestions() {
    const block = document.getElementById('skills-suggestions-block');
    const list = document.getElementById('skills-suggestions-list');
    const count = document.getElementById('skills-suggestions-count');
    if (!block || !list) return 0;
    const r = await api('/api/skills/suggestions');
    clear(list);
    if (r.data.error) {
      block.style.display = 'block';
      list.appendChild(el('div', { class: 'item' },
        el('div', { class: 'item-meta' }, r.data.error)));
      return 0;
    }
    const patterns = r.data.patterns || [];
    if (patterns.length === 0) {
      block.style.display = 'none';
      return 0;
    }
    block.style.display = 'block';
    if (count) count.textContent = `· ${patterns.length} pattern${patterns.length === 1 ? '' : 's'}`;
    for (const p of patterns) {
      const draftBtn = el('button',
        { type: 'button', class: 'secondary' }, 'draft');
      const declineBtn = el('button',
        { type: 'button', class: 'secondary' }, 'decline');
      const item = el('div', { class: 'item' },
        el('div', { class: 'item-title' },
          el('span', { class: 'tag' }, p.kind || ''),
          ' ',
          el('code', { style: 'font-size:0.85em;' }, p.id || ''),
          ' ',
          el('span', { style: 'color:#888; font-size:0.85em;' },
            `· ${p.occurrences} hits`)
        ),
        el('div', { class: 'item-meta' }, p.description || ''),
        el('div', { class: 'modal-actions',
                    style: 'margin-top:8px; gap:6px; justify-content:flex-start;' },
          draftBtn, declineBtn)
      );
      draftBtn.onclick = async () => {
        draftBtn.disabled = true;
        draftBtn.textContent = 'drafting…';
        try {
          const resp = await api(
            `/api/skills/suggestions/${encodeURIComponent(p.id)}/propose`,
            { method: 'POST', body: {} },
          );
          if (resp.data.error) {
            setFooter(`draft failed: ${resp.data.error}`);
            draftBtn.disabled = false;
            draftBtn.textContent = 'draft';
            return;
          }
          setFooter(`drafted: ${resp.data.name} (quarantined)`);
          // Re-mount the panel so the new draft appears in
          // installed list and pattern leaves the suggestions list.
          await panels.skills.mount();
        } catch (e) {
          setFooter(`draft failed: ${e.message}`);
          draftBtn.disabled = false;
          draftBtn.textContent = 'draft';
        }
      };
      declineBtn.onclick = async () => {
        declineBtn.disabled = true;
        declineBtn.textContent = 'declined';
        try {
          await api(
            `/api/skills/suggestions/${encodeURIComponent(p.id)}/decline`,
            { method: 'POST', body: {} },
          );
          setFooter(`silenced for ${r.data.cooldown_days || 7} days`);
          item.style.opacity = '0.4';
        } catch (e) {
          setFooter(`decline failed: ${e.message}`);
          declineBtn.disabled = false;
          declineBtn.textContent = 'decline';
        }
      };
      list.appendChild(item);
    }
    return patterns.length;
  }

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
        // v1.31.0: load suggestions + installed in parallel.
        const [suggestionCount, r] = await Promise.all([
          loadSkillSuggestions().catch(() => 0),
          api('/api/skills'),
        ]);
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
        const tail = suggestionCount > 0
          ? ` · ${suggestionCount} suggestion${suggestionCount === 1 ? '' : 's'}`
          : '';
        setFooter(`${skills.length} skill(s) listed${tail}`);
      };
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- files panel ----------

  const filesState = {
    currentDir: '.',
    currentFile: null,
    originalContent: '',
    editing: false,
    cmEditor: null,   // v1.24.1: CodeMirror instance
  };

  // v1.24.1: file-extension → CodeMirror mode lookup. Falls back to
  // null mode (plain text) for unknown extensions.
  const _CM_MODE_BY_EXT = {
    js: 'javascript', mjs: 'javascript', jsx: 'javascript',
    ts: 'javascript', tsx: 'javascript',
    py: 'python', pyi: 'python',
    md: 'markdown', markdown: 'markdown',
    html: 'htmlmixed', htm: 'htmlmixed',
    css: 'css', scss: 'css',
    xml: 'xml', svg: 'xml',
    yml: 'yaml', yaml: 'yaml',
    sh: 'shell', bash: 'shell', zsh: 'shell',
    json: { name: 'javascript', json: true },
  };

  function _cmModeFor(path) {
    const m = path.match(/\.([a-z0-9]+)$/i);
    if (!m) return null;
    return _CM_MODE_BY_EXT[m[1].toLowerCase()] || null;
  }

  function _disposeCM() {
    if (filesState.cmEditor) {
      try { filesState.cmEditor.toTextArea(); } catch (e) {}
      filesState.cmEditor = null;
    }
  }

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
            'select a file from the tree to view its contents'
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

      const renderFileView = (path, content, size) => {
        clear(viewer);
        filesState.originalContent = content || '';
        filesState.editing = false;
        const meta = el(
          'div',
          { class: 'file-actions' },
          el('span', { class: 'item-meta', style: 'flex:1;' },
            `${path}  ·  ${size} bytes`),
          el('button', { type: 'button', id: 'file-edit-btn' }, 'edit'),
        );
        viewer.appendChild(meta);
        viewer.appendChild(el('pre', { id: 'file-pre' }, content || ''));
        document.getElementById('file-edit-btn').onclick = () =>
          renderFileEdit(path);
      };

      const renderFileEdit = (path) => {
        clear(viewer);
        _disposeCM();
        filesState.editing = true;
        const dirty = el('span', { class: 'dirty', id: 'file-dirty' }, '');
        const saveBtn = el('button',
          { type: 'button', id: 'file-save-btn', class: 'primary' },
          'save');
        const cancelBtn = el('button',
          { type: 'button', id: 'file-cancel-btn' }, 'cancel');
        const modeLabel = el('span', {
          class: 'item-meta', style: 'font-family:ui-monospace,monospace;',
        }, '');
        const meta = el(
          'div', { class: 'file-actions' },
          el('span', { class: 'item-meta', style: 'flex:1;' }, `editing ${path}`),
          modeLabel, dirty, saveBtn, cancelBtn,
        );
        viewer.appendChild(meta);
        const ta = el('textarea', {
          id: 'file-textarea', spellcheck: 'false',
        }, filesState.originalContent);
        viewer.appendChild(ta);

        // v1.24.1: upgrade textarea to CodeMirror if available.
        const mode = _cmModeFor(path);
        const useCM = (typeof CodeMirror !== 'undefined');
        const getValue = () => useCM
          ? filesState.cmEditor.getValue()
          : ta.value;
        const setupCM = () => {
          try {
            filesState.cmEditor = CodeMirror.fromTextArea(ta, {
              mode: mode,
              lineNumbers: true,
              theme: 'dracula',
              indentUnit: 2,
              tabSize: 2,
              matchBrackets: true,
              autoCloseBrackets: true,
              lineWrapping: true,
              extraKeys: {
                'Ctrl-S': () => saveBtn.click(),
                'Cmd-S': () => saveBtn.click(),
              },
            });
            filesState.cmEditor.setSize('100%', '60vh');
            modeLabel.textContent = mode
              ? `mode: ${typeof mode === 'string' ? mode : mode.name}`
              : 'mode: text';
            filesState.cmEditor.on('change', () => {
              const v = filesState.cmEditor.getValue();
              const isDirty = v !== filesState.originalContent;
              dirty.textContent = isDirty ? '● modified' : '';
              saveBtn.disabled = !isDirty;
            });
            setTimeout(() => filesState.cmEditor.focus(), 30);
          } catch (e) {
            // CM init failed; fall back to plain textarea.
            filesState.cmEditor = null;
            modeLabel.textContent = '(CM unavailable)';
            setTimeout(() => ta.focus(), 30);
          }
        };
        if (useCM) {
          setupCM();
        } else {
          modeLabel.textContent = 'plain';
          setTimeout(() => ta.focus(), 30);
        }
        ta.oninput = () => {
          if (filesState.cmEditor) return;
          const isDirty = ta.value !== filesState.originalContent;
          dirty.textContent = isDirty ? '● modified' : '';
          saveBtn.disabled = !isDirty;
        };
        ta.onkeydown = (e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            saveBtn.click();
          }
        };
        cancelBtn.onclick = () => {
          if (getValue() !== filesState.originalContent) {
            if (!confirm('discard your edits?')) return;
          }
          _disposeCM();
          renderFileView(path, filesState.originalContent,
            filesState.originalContent.length);
        };
        saveBtn.onclick = async () => {
          const content = getValue();
          saveBtn.disabled = true;
          dirty.textContent = 'saving...';
          const r = await api('/api/files/write', {
            method: 'POST',
            body: { path: path, content: content },
          });
          if (r.data.error) {
            dirty.textContent = '';
            alert('save failed: ' + r.data.error);
            saveBtn.disabled = false;
            return;
          }
          filesState.originalContent = content;
          _disposeCM();
          renderFileView(r.data.path || path, content,
            r.data.size || content.length);
          setFooter('saved ' + (r.data.path || path));
        };
      };

      const loadFile = async (path, node) => {
        document
          .querySelectorAll('#files-tree .file-entry.active')
          .forEach((n) => n.classList.remove('active'));
        if (node) node.classList.add('active');
        clear(viewer);
        viewer.appendChild(el('div', { class: 'empty' }, 'loading ' + path + '...'));
        const r = await api('/api/files/read?path=' + encodeURIComponent(path));
        if (r.data.error) {
          clear(viewer);
          viewer.appendChild(el('div', { class: 'empty' }, r.data.error));
          return;
        }
        filesState.currentFile = path;
        renderFileView(r.data.path || path, r.data.content || '', r.data.size || 0);
        setFooter('viewing ' + path);
      };

      renderEmpty();
      await loadDir('.');
    },
  });

  // ---------- v1.22.1: interview panel ----------

  registerPanel('interview', {
    async mount() {
      const stateDiv = document.getElementById('interview-state');
      const meterDiv = document.getElementById('interview-meter');
      const start = document.getElementById('interview-start-drip');
      const pause = document.getElementById('interview-pause');
      const aboutme = document.getElementById('interview-aboutme');
      const refresh = document.getElementById('interview-refresh');
      if (!stateDiv) return;
      const renderMeter = (completion, total) => {
        clear(meterDiv);
        const cats = Object.keys(completion || {}).sort();
        if (!cats.length) {
          meterDiv.appendChild(el('div', { class: 'item-meta' }, 'no completion data'));
          return;
        }
        let totalPct = 0;
        for (const cat of cats) {
          const pct = Math.round((completion[cat] || 0) * 100);
          totalPct += pct;
          const bar = el('div', { style: 'display:flex; align-items:center; gap:10px; margin-bottom:6px;' },
            el('span', { style: 'width:120px; font-family:ui-monospace,monospace; font-size:0.85em;' }, cat),
            el('div', { style: 'width:240px; height:10px; background:#eee; border-radius:5px; overflow:hidden;' },
              el('div', { style: `width:${pct}%; height:100%; background:var(--brand);` })
            ),
            el('span', { style: 'font-family:ui-monospace,monospace; font-size:0.85em; color:#666;' }, pct + '%')
          );
          meterDiv.appendChild(bar);
        }
        const overall = cats.length ? Math.round(totalPct / cats.length) : 0;
        meterDiv.appendChild(el('div', { style: 'margin-top:14px; font-weight:600;' },
          'overall: ' + overall + '%'));
      };
      const load = async () => {
        clear(stateDiv);
        const sessionId = chatState.sessionId;
        const r = await api('/api/interview/state?session_id=' + encodeURIComponent(sessionId));
        if (r.data.error) {
          stateDiv.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, r.data.error)));
          return;
        }
        stateDiv.appendChild(el('div', { class: 'item' },
          el('div', { class: 'item-title' },
            'mode: ', el('span', { class: 'tag' }, r.data.mode || 'idle')
          ),
          el('div', { class: 'item-meta' },
            `answered ${r.data.answered_count} · skipped ${r.data.skipped_count} · ` +
            `quota ${r.data.drip_quota_remaining} · filter "${r.data.drip_filter_category || 'all'}"`
          )
        ));
        renderMeter(r.data.completion);
      };
      if (refresh) refresh.onclick = load;
      if (start) start.onclick = async () => {
        const sessionId = chatState.sessionId;
        await api('/api/interview/start', {
          method: 'POST',
          body: { session_id: sessionId, daily_count: 2 },
        });
        load();
      };
      if (pause) pause.onclick = async () => {
        const sessionId = chatState.sessionId;
        await api('/api/interview/pause', {
          method: 'POST', body: { session_id: sessionId },
        });
        load();
      };
      if (aboutme) aboutme.onclick = async () => {
        const r = await api('/api/interview/about-me');
        clear(stateDiv);
        stateDiv.appendChild(el('div', { class: 'item' },
          el('div', { class: 'item-body' }, (r.data && r.data.body) || r.data.error || '(empty)')
        ));
      };
      await load();
    },
  });

  // ---------- v1.22.2: agents panel ----------

  registerPanel('agents', {
    async mount() {
      const list = document.getElementById('agents-list');
      const refresh = document.getElementById('agents-refresh');
      if (!list) return;
      const load = async () => {
        clear(list);
        const r = await api('/api/agents');
        if (r.data.error) {
          list.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, r.data.error)));
          return;
        }
        // agent_list output is a string; render as preformatted text.
        list.appendChild(el('div', { class: 'item' },
          el('div', { class: 'item-body' }, r.data.output || '(no agents)')
        ));
      };
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- v1.22.2: swarms panel ----------

  registerPanel('swarms', {
    async mount() {
      const specs = document.getElementById('swarms-specs');
      const runs = document.getElementById('swarms-runs');
      const refresh = document.getElementById('swarms-refresh');
      const load = async () => {
        clear(specs);
        clear(runs);
        const sr = await api('/api/swarms/specs');
        if (sr.data.specs) {
          if (!sr.data.specs.length) {
            specs.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, '(no specs)')));
          } else {
            for (const s of sr.data.specs) {
              specs.appendChild(el('div', { class: 'item' },
                el('div', { class: 'item-title' }, s.name),
                el('div', { class: 'item-meta' },
                  `${s.phases} phases · max ${s.max_subagents || '-'} agents · $${s.max_budget_usd || '-'}`),
                el('div', { class: 'item-body' }, s.description || '')
              ));
            }
          }
        }
        const rr = await api('/api/swarms/runs');
        if (rr.data.runs) {
          if (!rr.data.runs.length) {
            runs.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, '(no runs)')));
          } else {
            for (const id of rr.data.runs) {
              runs.appendChild(el('div', { class: 'item' },
                el('div', { class: 'item-title' }, id)
              ));
            }
          }
        }
      };
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- v1.22.2: triggers panel ----------

  registerPanel('triggers', {
    async mount() {
      const list = document.getElementById('triggers-list');
      const refresh = document.getElementById('triggers-refresh');
      if (!list) return;
      const load = async () => {
        clear(list);
        const r = await api('/api/triggers');
        if (r.data.error) {
          list.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, r.data.error)));
          return;
        }
        if (!r.data.triggers.length) {
          list.appendChild(el('div', { class: 'item' },
            el('div', { class: 'item-meta' }, '(no triggers)')
          ));
          return;
        }
        for (const t of r.data.triggers) {
          list.appendChild(el('div', { class: 'item' },
            el('div', { class: 'item-title' },
              el('span', { class: 'tag ' + (t.enabled ? 'promoted' : 'quarantined') },
                t.enabled ? 'on' : 'off'),
              ' ',
              t.name
            ),
            el('div', { class: 'item-meta' },
              `${t.kind || ''} · when: ${t.when || ''} · skill: ${t.skill || ''} · → ${t.deliver_to || ''}`)
          ));
        }
      };
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- v1.22.3 + v1.24.0: shells panel with xterm.js ----------

  const shellsState = {
    term: null,        // xterm.js Terminal instance
    fitAddon: null,
    eventSource: null, // active SSE stream
    activeShellId: null,
  };

  function _ensureTerminal() {
    const wrap = document.getElementById('shells-terminal');
    if (!wrap || shellsState.term) return shellsState.term;
    if (typeof Terminal === 'undefined') {
      // xterm.js missing — fall back to plain pre.
      wrap.innerHTML = '<pre style="color:#fff; font-size:0.85em;">'
        + '(xterm.js missing; terminal unavailable)</pre>';
      return null;
    }
    const term = new Terminal({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
      fontSize: 13,
      theme: {
        background: '#1e1e1e',
        foreground: '#e0e0e0',
        cursor: '#a020f0',
      },
      cursorBlink: false,
      convertEol: true,    // \n -> \r\n
      scrollback: 5000,
      disableStdin: true,  // v1.24.0 is read-only viewer
    });
    let fitAddon = null;
    try {
      fitAddon = new FitAddon.FitAddon();
      term.loadAddon(fitAddon);
    } catch (e) {
      // Fit addon unavailable — fall back to fixed cols/rows.
    }
    term.open(wrap);
    if (fitAddon) {
      try { fitAddon.fit(); } catch (e) {}
      window.addEventListener('resize', () => {
        try { fitAddon.fit(); } catch (e) {}
      });
    }
    shellsState.term = term;
    shellsState.fitAddon = fitAddon;
    return term;
  }

  function _closeStream() {
    if (shellsState.eventSource) {
      try { shellsState.eventSource.close(); } catch (e) {}
      shellsState.eventSource = null;
    }
  }

  // v1.24.1: shellsState tracks PTY mode so onData wiring is conditional.
  shellsState.activePty = false;
  shellsState.dataDisposable = null;

  async function _sendStdin(shellId, data) {
    try {
      await api('/api/shells/' + encodeURIComponent(shellId) + '/stdin', {
        method: 'POST',
        body: { data: data },
      });
    } catch (e) {
      // Fail silently — the user will notice a hang and can refresh.
    }
  }

  function _attachStream(shellId, isPty) {
    _closeStream();
    const term = _ensureTerminal();
    if (!term) return;
    if (shellsState.dataDisposable) {
      try { shellsState.dataDisposable.dispose(); } catch (e) {}
      shellsState.dataDisposable = null;
    }
    term.clear();
    const ptyTag = isPty ? ' \x1b[35m[PTY]\x1b[0m' : '';
    term.write(`\x1b[36m[attached to ${shellId}]\x1b[0m${ptyTag}\r\n`);
    shellsState.activeShellId = shellId;
    shellsState.activePty = !!isPty;

    // v1.24.1: PTY shells accept stdin. Toggle xterm's read-only flag
    // and pipe keystrokes to the stdin endpoint.
    if (isPty) {
      try { term.options.disableStdin = false; } catch (e) {}
      shellsState.dataDisposable = term.onData((data) => {
        _sendStdin(shellId, data);
      });
    } else {
      try { term.options.disableStdin = true; } catch (e) {}
    }

    let es;
    try {
      es = new EventSource(
        '/api/shells/' + encodeURIComponent(shellId) + '/stream',
        { withCredentials: true },
      );
    } catch (e) {
      term.write(`\x1b[31m[stream unsupported]\x1b[0m\r\n`);
      return;
    }
    es.addEventListener('chunk', (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.text) term.write(data.text);
      } catch (err) {}
    });
    es.addEventListener('end', (e) => {
      try {
        const data = JSON.parse(e.data);
        term.write(`\r\n\x1b[33m[shell ended: ${data.status}]\x1b[0m\r\n`);
      } catch {}
      _closeStream();
    });
    es.onerror = () => {
      // EventSource auto-reconnects until close(); we don't intervene.
    };
    shellsState.eventSource = es;
  }

  registerPanel('shells', {
    async mount() {
      const list = document.getElementById('shells-list');
      const cmd = document.getElementById('shell-cmd');
      const runBtn = document.getElementById('shell-run');
      const refresh = document.getElementById('shells-refresh');
      if (!list) return;
      _ensureTerminal();
      const loadList = async () => {
        clear(list);
        const r = await api('/api/shells');
        if (r.data.error) {
          list.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, r.data.error)));
          return;
        }
        const txt = r.data.output || '';
        const lines = txt.split('\n').filter(Boolean);
        if (!lines.length) {
          list.appendChild(el('div', { class: 'item' }, el('div', { class: 'item-meta' }, '(no shells)')));
          return;
        }
        for (const line of lines) {
          const item = el('div', { class: 'item' }, el('div', { class: 'item-meta' }, line));
          const m = line.match(/(sh-[0-9a-f]{4,})/i);
          if (m) {
            const id = m[1];
            item.style.cursor = 'pointer';
            if (id === shellsState.activeShellId) {
              item.classList.add('active');
              item.style.background = '#f5edff';
            }
            item.onclick = () => _attachStream(id);
          }
          list.appendChild(item);
        }
      };
      if (refresh) refresh.onclick = loadList;
      const ptyCheckbox = document.getElementById('shell-pty');
      if (runBtn) runBtn.onclick = async () => {
        const c = (cmd ? cmd.value : '').trim();
        if (!c) return;
        const usePty = !!(ptyCheckbox && ptyCheckbox.checked);
        const r = await api('/api/shells/run', {
          method: 'POST', body: { command: c, pty: usePty },
        });
        if (cmd) cmd.value = '';
        const out = (r.data && (r.data.output || r.data.error)) || '';
        // Server may return shell_id directly (PTY path) or embed it
        // in the legacy ShellRunBg output text.
        const id = (r.data && r.data.shell_id)
                || (out.match(/(sh-[0-9a-f]{4,})/i) || [])[1];
        if (r.data && r.data.error) {
          alert(r.data.error);
        } else if (id) {
          _attachStream(id, !!(r.data && r.data.pty));
        }
        loadList();
      };
      if (cmd) cmd.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); runBtn.click(); }
      };
      await loadList();
    },
  });

  // ---------- v1.22.3 + v1.24.1: logs panel (live SSE) ----------

  const logsState = {
    eventSource: null,
    list: null,
    capacity: 200,  // max items kept in DOM
  };

  function _renderLogEntry(parsed) {
    const ts = parsed.ts || '';
    const gw_ = parsed.gateway || '';
    const mode = parsed.mode || '';
    const tool = parsed.tool || '';
    const summary = (parsed.request || parsed.error || parsed.output ||
                     parsed.type || '');
    return el(
      'div', { class: 'item' },
      el('div', { class: 'item-meta' }, `${ts}  ${gw_}  ${mode}  ${tool}`),
      el('div', { class: 'item-body' }, String(summary).slice(0, 400)),
    );
  }

  function _logsCloseStream() {
    if (logsState.eventSource) {
      try { logsState.eventSource.close(); } catch (e) {}
      logsState.eventSource = null;
    }
  }

  function _logsAttachStream() {
    _logsCloseStream();
    if (!logsState.list) return;
    let es;
    try {
      es = new EventSource('/api/logs/stream', { withCredentials: true });
    } catch (e) {
      logsState.list.prepend(el(
        'div', { class: 'item' },
        el('div', { class: 'item-meta' }, '(SSE unsupported)'),
      ));
      return;
    }
    es.addEventListener('entry', (e) => {
      try {
        const data = JSON.parse(e.data);
        const node = _renderLogEntry(data);
        // Append to TOP so newest is visible. Trim DOM after capacity.
        if (logsState.list.firstChild) {
          logsState.list.insertBefore(node, logsState.list.firstChild);
        } else {
          logsState.list.appendChild(node);
        }
        while (logsState.list.children.length > logsState.capacity) {
          logsState.list.removeChild(logsState.list.lastChild);
        }
      } catch (err) {}
    });
    es.addEventListener('error', () => {
      // EventSource auto-reconnects; suppress noise.
    });
    logsState.eventSource = es;
  }

  registerPanel('logs', {
    async mount() {
      const list = document.getElementById('logs-list');
      const refresh = document.getElementById('logs-refresh');
      if (!list) return;
      logsState.list = list;
      const reload = async () => {
        clear(list);
        // Re-attach SSE stream — its bootstrap sends the last 20.
        _logsAttachStream();
      };
      if (refresh) refresh.onclick = reload;
      await reload();
    },
  });

  // ---------- v1.22.3 + v1.31.1: cost panel ----------

  // v1.31.1: render the budget gauge from /api/cost-detail's budget
  // block. Hides the gauge entirely when JANUS_BUDGET_USD isn't set
  // (configured=false) — no point showing an empty 0/0 bar.
  function renderCostBudget(budget) {
    const block = document.getElementById('cost-budget-block');
    const fill = document.getElementById('cost-gauge-fill');
    const label = document.getElementById('cost-budget-label');
    const state = document.getElementById('cost-budget-state');
    if (!block || !fill) return;
    if (!budget || !budget.configured) {
      block.style.display = 'none';
      return;
    }
    block.style.display = 'block';
    const pct = Math.max(0, budget.percent || 0);
    fill.style.width = Math.min(pct, 1.5) * 100 + '%';
    fill.classList.toggle('over', pct >= 1.0);
    if (label) {
      label.textContent =
        '$' + (budget.spent || 0).toFixed(4) +
        ' / $' + (budget.budget || 0).toFixed(2) +
        ' (' + (pct * 100).toFixed(1) + '%)';
    }
    if (state) {
      let tag = 'ok';
      let text = 'within budget';
      if (pct >= 1.0) { tag = 'over'; text = 'OVER'; }
      else if (pct >= 0.8) { tag = 'warn'; text = '≥80%'; }
      else if (pct >= 0.5) { tag = 'warn'; text = '≥50%'; }
      state.className = 'tag gauge-' + tag;
      state.textContent = text;
    }
  }

  // v1.31.1: render daily-rollup as inline SVG bar chart. Pure
  // hand-rolled — no charting lib pulled in. Y-axis: USD; X-axis:
  // each day with date label every other bar (avoid overlap on
  // 14/30/90-day windows).
  function renderCostChart(daily) {
    const svg = document.getElementById('cost-chart');
    const empty = document.getElementById('cost-chart-empty');
    const totals = document.getElementById('cost-chart-totals');
    const block = document.getElementById('cost-chart-block');
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!Array.isArray(daily) || daily.length === 0) {
      if (empty) empty.style.display = 'block';
      if (totals) totals.textContent = '';
      svg.style.display = 'none';
      return;
    }
    if (empty) empty.style.display = 'none';
    svg.style.display = 'block';

    // daily is newest-first per cost.daily_totals; flip for chart order.
    const series = daily.slice().reverse();
    const sumUsd = series.reduce((a, b) => a + (b.usd || 0), 0);
    const sumCalls = series.reduce((a, b) => a + (b.calls || 0), 0);
    const maxUsd = Math.max(...series.map((d) => d.usd || 0), 0.0001);

    if (totals) {
      totals.textContent =
        series.length + ' day(s) · ' +
        sumCalls + ' call(s) · $' + sumUsd.toFixed(4);
    }

    // viewBox 600x160; reserve 24px bottom for date labels and 30px
    // left for usd axis labels.
    const W = 600, H = 160, pad_l = 30, pad_b = 24, pad_t = 8, pad_r = 8;
    const plot_w = W - pad_l - pad_r;
    const plot_h = H - pad_b - pad_t;
    const n = series.length;
    const bar_gap = 2;
    const bar_w = Math.max(1, plot_w / n - bar_gap);

    const ns = 'http://www.w3.org/2000/svg';

    // Axis line (bottom)
    const axis = document.createElementNS(ns, 'line');
    axis.setAttribute('x1', pad_l);
    axis.setAttribute('y1', pad_t + plot_h);
    axis.setAttribute('x2', W - pad_r);
    axis.setAttribute('y2', pad_t + plot_h);
    axis.setAttribute('class', 'cost-axis-line');
    svg.appendChild(axis);

    // Y-axis: max + half labels.
    const yMaxLabel = document.createElementNS(ns, 'text');
    yMaxLabel.setAttribute('x', pad_l - 4);
    yMaxLabel.setAttribute('y', pad_t + 4);
    yMaxLabel.setAttribute('class', 'cost-axis-label');
    yMaxLabel.setAttribute('text-anchor', 'end');
    yMaxLabel.textContent = '$' + maxUsd.toFixed(maxUsd < 0.01 ? 4 : 2);
    svg.appendChild(yMaxLabel);

    const yMidLabel = document.createElementNS(ns, 'text');
    yMidLabel.setAttribute('x', pad_l - 4);
    yMidLabel.setAttribute('y', pad_t + plot_h / 2 + 4);
    yMidLabel.setAttribute('class', 'cost-axis-label');
    yMidLabel.setAttribute('text-anchor', 'end');
    yMidLabel.textContent = '$' + (maxUsd / 2).toFixed(maxUsd < 0.01 ? 4 : 2);
    svg.appendChild(yMidLabel);

    // Bars + date labels.
    series.forEach((d, i) => {
      const x = pad_l + i * (plot_w / n) + bar_gap / 2;
      const usd = d.usd || 0;
      const h = (usd / maxUsd) * plot_h;
      const y = pad_t + plot_h - h;
      const rect = document.createElementNS(ns, 'rect');
      rect.setAttribute('x', x);
      rect.setAttribute('y', y);
      rect.setAttribute('width', bar_w);
      rect.setAttribute('height', Math.max(0, h));
      rect.setAttribute('class', 'cost-bar');
      // Title hover for accessible tooltips
      const title = document.createElementNS(ns, 'title');
      title.textContent =
        d.date + '  ·  $' + usd.toFixed(4) +
        '  ·  ' + (d.calls || 0) + ' call(s)';
      rect.appendChild(title);
      svg.appendChild(rect);

      // Date label every Nth bar to avoid overlap.
      const stride = Math.ceil(n / 8);
      if (i % stride === 0 || i === n - 1) {
        const txt = document.createElementNS(ns, 'text');
        txt.setAttribute('x', x + bar_w / 2);
        txt.setAttribute('y', H - 8);
        txt.setAttribute('class', 'cost-axis-label');
        txt.setAttribute('text-anchor', 'middle');
        // Show MM-DD (drop year for compactness)
        txt.textContent = (d.date || '').slice(5);
        svg.appendChild(txt);
      }
    });
    if (block) block.style.display = 'block';
  }

  registerPanel('cost', {
    async mount() {
      const summary = document.getElementById('cost-summary');
      const refresh = document.getElementById('cost-refresh');
      const windowSel = document.getElementById('cost-window');
      if (!summary) return;
      const load = async () => {
        summary.textContent = 'loading...';
        const days = parseInt((windowSel && windowSel.value) || '14', 10);
        const r = await api('/api/cost-detail?days=' + days);
        if (r.data && r.data.error) {
          summary.textContent = r.data.error;
          renderCostBudget(null);
          renderCostChart([]);
          return;
        }
        summary.textContent = (r.data && r.data.summary) || '(empty)';
        renderCostBudget(r.data && r.data.budget);
        renderCostChart((r.data && r.data.daily) || []);
      };
      if (refresh) refresh.onclick = load;
      if (windowSel) windowSel.onchange = load;
      await load();
    },
  });

  // ---------- v1.29.4: MCP catalog browser panel ----------

  registerPanel('mcp', {
    async mount() {
      const list = document.getElementById('mcp-list');
      const refresh = document.getElementById('mcp-refresh');
      if (!list) return;
      const load = async () => {
        clear(list);
        list.appendChild(
          el('div', { class: 'item' },
             el('div', { class: 'item-meta' }, 'loading...'))
        );
        const r = await api('/api/mcp/catalog');
        clear(list);
        if (r.data.error) {
          list.appendChild(
            el('div', { class: 'item' },
               el('div', { class: 'item-meta' }, r.data.error))
          );
          return;
        }
        const servers = r.data.servers || [];
        if (servers.length === 0) {
          list.appendChild(
            el('div', { class: 'item' },
               el('div', { class: 'item-meta' },
                  'no MCP servers configured — add to ~/.janus/mcp/servers.json'))
          );
          return;
        }
        for (const s of servers) {
          const stateClass = s.connected ? 'promoted' : 'quarantined';
          const stateLabel = s.connected ? 'connected' : 'configured';
          const item = el(
            'div', { class: 'item' },
            el(
              'div', { class: 'item-title' },
              el('span', { class: 'tag ' + stateClass }, stateLabel),
              ' ',
              s.name
            ),
            el(
              'div', { class: 'item-meta' },
              `${s.command} ${(s.args || []).join(' ')}`
            )
          );
          if (s.error) {
            item.appendChild(
              el('div', { class: 'item-meta', style: 'color:#c43;' },
                 'error: ' + s.error)
            );
          }
          if (s.connected && s.tools && s.tools.length > 0) {
            const toolListEl = el(
              'div',
              { class: 'item-meta',
                style: 'margin-top:6px; font-family:ui-monospace,monospace;' }
            );
            for (const t of s.tools) {
              toolListEl.appendChild(
                el('div', { style: 'margin-bottom:4px;' },
                   el('strong', {}, t.name),
                   ` · ${t.param_count} param${t.param_count === 1 ? '' : 's'}`,
                   ' — ',
                   (t.description || '').slice(0, 100),
                   el('br', {}),
                   el('span', { style: 'color:#888; font-size:0.85em;' },
                      `janus name: ${t.janus_name}`))
              );
            }
            item.appendChild(toolListEl);
          } else if (s.connected) {
            item.appendChild(
              el('div', { class: 'item-meta' }, '(no tools exposed)')
            );
          }
          list.appendChild(item);
        }
        const liveCount = servers.filter(s => s.connected).length;
        setFooter(
          `${servers.length} server(s) · ${liveCount} connected`
        );
      };
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- v1.22.3: settings panel ----------

  registerPanel('settings', {
    async mount() {
      const view = document.getElementById('settings-view');
      const refresh = document.getElementById('settings-refresh');
      if (!view) return;
      const load = async () => {
        view.textContent = 'loading...';
        const r = await api('/api/settings');
        if (r.data.error) {
          view.textContent = r.data.error;
          return;
        }
        const lines = [];
        for (const [k, v] of Object.entries(r.data)) {
          lines.push(`${k.padEnd(24)} ${v}`);
        }
        view.textContent = lines.join('\n');
      };
      if (refresh) refresh.onclick = load;
      await load();
    },
  });

  // ---------- v1.22.0a: SSE event stream + approval/clarify modals ----------

  const modalState = {
    backdrop: null,
    approval: null,
    clarify: null,
    plan: null,        // v1.30.0
    activeRequestId: null,
    activeKind: null, // 'approval' | 'clarify' | 'plan'
  };

  function showApprovalModal(evt) {
    if (!modalState.backdrop) return;
    modalState.activeRequestId = evt.request_id;
    modalState.activeKind = 'approval';
    document.getElementById('approval-label').textContent = evt.label || '';
    document.getElementById('approval-details').textContent = evt.details || '';
    const riskTag = document.getElementById('approval-risk');
    riskTag.textContent = evt.risk || '';
    riskTag.className = 'tag risk-' + (evt.risk || 'ask');
    modalState.clarify.style.display = 'none';
    if (modalState.plan) modalState.plan.style.display = 'none';
    modalState.approval.style.display = 'block';
    modalState.backdrop.style.display = 'flex';
  }

  // v1.30.0: dedicated plan-review modal. Hooked from
  // handleSSEEvent('approval_pending') when the event payload carries
  // a `plan` key (built server-side via plan_render.build_web_payload).
  // Resolves through the same /api/approve/{id} POST as a generic
  // approval — only the rendering differs. No session/always grants
  // are offered (every plan deserves a fresh decision, matching
  // v1.27.2 cli_rich narrowing).
  function showPlanModal(evt) {
    if (!modalState.backdrop || !modalState.plan) {
      // Fallback: no plan modal shipped → render via generic approval.
      showApprovalModal(evt);
      return;
    }
    modalState.activeRequestId = evt.request_id;
    modalState.activeKind = 'plan';
    const plan = evt.plan || {};
    const metric = document.getElementById('plan-metrics');
    if (metric) metric.textContent = plan.metric_line || '';
    const modeTag = document.getElementById('plan-mode-tag');
    if (modeTag) modeTag.textContent = 'mode=' + (plan.mode || 'plan');

    // Files as chips
    const filesDiv = document.getElementById('plan-files');
    if (filesDiv) {
      clear(filesDiv);
      const files = Array.isArray(plan.files) ? plan.files : [];
      for (const f of files) {
        filesDiv.appendChild(el('span', { class: 'file-chip' }, f));
      }
      const remaining = (plan.file_count || 0) - files.length;
      if (remaining > 0 || plan.files_truncated) {
        const more = remaining > 0 ? '+' + remaining + ' more' : 'more files…';
        filesDiv.appendChild(el('span', { class: 'file-chip more' }, more));
      }
    }

    // Steps as numbered list
    const stepsList = document.getElementById('plan-steps');
    if (stepsList) {
      clear(stepsList);
      const steps = Array.isArray(plan.steps) ? plan.steps : [];
      for (const s of steps) {
        stepsList.appendChild(el('li', {}, s));
      }
    }

    // Raw plan body (markdown left as-is — keeps it copyable)
    const body = document.getElementById('plan-body');
    if (body) body.textContent = plan.body_md || evt.details || '';

    if (modalState.approval) modalState.approval.style.display = 'none';
    if (modalState.clarify) modalState.clarify.style.display = 'none';
    modalState.plan.style.display = 'block';
    modalState.backdrop.style.display = 'flex';
  }

  function showClarifyModal(evt) {
    if (!modalState.backdrop) return;
    modalState.activeRequestId = evt.request_id;
    modalState.activeKind = 'clarify';
    document.getElementById('clarify-question').textContent = evt.question || '';
    const choicesDiv = document.getElementById('clarify-choices');
    clear(choicesDiv);
    if (evt.choices && evt.choices.length) {
      for (const choice of evt.choices) {
        const btn = el('button', { type: 'button' }, choice);
        btn.onclick = () => submitClarify(choice);
        choicesDiv.appendChild(btn);
      }
    }
    const text = document.getElementById('clarify-text');
    if (text) {
      text.value = '';
      // Focus shortly after display so the input is ready.
      setTimeout(() => text.focus(), 30);
    }
    modalState.approval.style.display = 'none';
    if (modalState.plan) modalState.plan.style.display = 'none';
    modalState.clarify.style.display = 'block';
    modalState.backdrop.style.display = 'flex';
  }

  function hideModal() {
    if (modalState.backdrop) modalState.backdrop.style.display = 'none';
    modalState.activeRequestId = null;
    modalState.activeKind = null;
  }

  async function submitApproval(decision) {
    // v1.30.0: also resolve plan-modal decisions — same POST endpoint.
    if (!modalState.activeRequestId) return;
    if (modalState.activeKind !== 'approval' && modalState.activeKind !== 'plan') return;
    const id = modalState.activeRequestId;
    hideModal();
    try {
      await api('/api/approve/' + encodeURIComponent(id), {
        method: 'POST',
        body: { approve: decision },
      });
    } catch (e) {
      setFooter('approval send failed: ' + e.message);
    }
  }

  async function submitClarify(answer) {
    if (!modalState.activeRequestId || modalState.activeKind !== 'clarify') return;
    const id = modalState.activeRequestId;
    hideModal();
    try {
      await api('/api/clarify/' + encodeURIComponent(id), {
        method: 'POST',
        body: { answer: String(answer || '') },
      });
    } catch (e) {
      setFooter('clarify send failed: ' + e.message);
    }
  }

  function handleSSEEvent(evt, data) {
    const sw = document.getElementById('footer-events');
    if (evt === 'approval_pending') {
      // v1.30.0: route ExitPlanMode approvals (those with a `plan`
      // payload) to the dedicated plan-review modal.
      if (data && data.plan) {
        showPlanModal(data);
      } else {
        showApprovalModal(data);
      }
    } else if (evt === 'clarify_pending') {
      showClarifyModal(data);
    } else if (evt === 'approval_resolved' || evt === 'clarify_resolved') {
      // Another tab resolved this request — dismiss our modal.
      if (modalState.activeRequestId === data.request_id) hideModal();
    }
    if (sw) sw.textContent = 'events: live';
  }

  function startEventStream() {
    const sw = document.getElementById('footer-events');
    let es;
    try {
      es = new EventSource('/api/events', { withCredentials: true });
    } catch (e) {
      if (sw) sw.textContent = 'events: unsupported';
      return;
    }
    es.addEventListener('approval_pending', (e) => {
      try { handleSSEEvent('approval_pending', JSON.parse(e.data)); } catch {}
    });
    es.addEventListener('clarify_pending', (e) => {
      try { handleSSEEvent('clarify_pending', JSON.parse(e.data)); } catch {}
    });
    es.addEventListener('approval_resolved', (e) => {
      try { handleSSEEvent('approval_resolved', JSON.parse(e.data)); } catch {}
    });
    es.addEventListener('clarify_resolved', (e) => {
      try { handleSSEEvent('clarify_resolved', JSON.parse(e.data)); } catch {}
    });
    // v1.24.1: memory.changed → re-mount the memory panel if it's active.
    es.addEventListener('memory.changed', () => {
      const memPanel = document.getElementById('panel-memory');
      if (memPanel && memPanel.classList.contains('active')) {
        try { panels.memory.mount(); } catch (e) {}
      }
    });
    es.onopen = () => { if (sw) sw.textContent = 'events: connected'; };
    es.onerror = () => {
      if (sw) sw.textContent = 'events: reconnecting...';
      // EventSource auto-reconnects; nothing else to do.
    };
  }

  function setupModals() {
    modalState.backdrop = document.getElementById('modal-backdrop');
    modalState.approval = document.getElementById('modal-approval');
    modalState.clarify = document.getElementById('modal-clarify');
    modalState.plan = document.getElementById('modal-plan');
    if (!modalState.backdrop) return;
    document.getElementById('approval-approve').onclick = () => submitApproval(true);
    document.getElementById('approval-deny').onclick = () => submitApproval(false);
    // v1.30.0: plan-review modal buttons reuse submitApproval (same
    // /api/approve/{id} POST). The activeKind === 'plan' branch in
    // submitApproval lets it through.
    const planApprove = document.getElementById('plan-approve');
    const planDeny = document.getElementById('plan-deny');
    if (planApprove) planApprove.onclick = () => submitApproval(true);
    if (planDeny) planDeny.onclick = () => submitApproval(false);
    document.getElementById('clarify-submit').onclick = () => {
      const text = document.getElementById('clarify-text');
      submitClarify(text ? text.value : '');
    };
    document.getElementById('clarify-cancel').onclick = () => submitClarify('');
    const text = document.getElementById('clarify-text');
    if (text) {
      text.onkeydown = (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          submitClarify(text.value);
        } else if (e.key === 'Escape') {
          submitClarify('');
        }
      };
    }
  }

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
    setupModals();
    startEventStream();
    onHashChange();
  });
})();
