// The onboarding help terminal: docs/superpowers/specs/2026-11-12-onboarding-help-terminal-panel.md §3
(function () {
  'use strict';
  var panel = document.getElementById('help-panel');
  if (!panel || !window.Terminal) return;
  var draft = panel.dataset.draft;
  var KEY = 'jarvis-help:' + draft, WIDTH_KEY = 'jarvis-help-width';
  var statusEl = document.getElementById('help-status');
  var againBtn = document.getElementById('help-again');
  var forms = Array.prototype.slice.call(document.querySelectorAll('form[action$="/help"]'));
  var term = null, fit = null, ws = null, current = null;

  function store(op, value) {
    // sessionStorage/localStorage can throw (private mode, blocked site data): degrade, never break
    try {
      var s = op === 'width' ? localStorage : sessionStorage, k = op === 'width' ? WIDTH_KEY : KEY;
      if (value === undefined) return JSON.parse(s.getItem(k) || 'null');
      if (value === null) s.removeItem(k); else s.setItem(k, JSON.stringify(value));
    } catch (e) { return null; }
  }

  function status(text) { statusEl.textContent = text; }

  function clampWidth(px) {
    return Math.max(280, Math.min(px, window.innerWidth * 0.7));
  }

  function setWidth(px) {
    document.documentElement.style.setProperty('--help-w', clampWidth(px) + 'px');
  }

  function sendSize() {
    if (!fit) return;
    try { fit.fit(); } catch (e) { return; }
    if (ws && ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({resize: [term.rows, term.cols]}));
  }

  function show() {
    panel.hidden = false;
    document.body.classList.add('help-open');
    var w = store('width');
    if (typeof w === 'number') setWidth(w);
    if (!term) {
      term = new window.Terminal({convertEol: false, cursorBlink: true, fontSize: 13,
                                  scrollback: 5000});
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(document.getElementById('help-term'));
      term.onData(function (d) {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(d));
      });
      window.helpTerm = term;  // the browser test reads term.buffer.active
    }
    sendSize();
  }

  function hide() {
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    current = null;
    panel.hidden = true;
    document.body.classList.remove('help-open');
    if (term) { term.dispose(); term = null; fit = null; window.helpTerm = null; }
  }

  function connect(entry) {
    current = entry;
    show();
    againBtn.hidden = true;
    document.getElementById('help-name').textContent = entry.session_name || draft;
    status('connecting');
    if (ws) { ws.onclose = null; ws.close(); }
    term.reset();  // the server's first frame is the whole scrollback
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var sock = new WebSocket(proto + location.host + entry.ws_url + '?token=' +
                             encodeURIComponent(entry.token));
    sock.binaryType = 'arraybuffer';
    ws = sock;
    sock.onopen = function () { status('live'); sendSize(); term.focus(); };
    sock.onmessage = function (ev) {
      if (term) term.write(typeof ev.data === 'string' ? ev.data : new Uint8Array(ev.data));
    };
    sock.onclose = function (ev) {
      if (ws !== sock) return;
      ws = null;
      if (ev.code === 4404 || ev.code === 4403) { store('entry', null); hide(); }
      else if (ev.code === 1000) { store('entry', null); status('help session ended'); againBtn.hidden = false; }
      else if (ev.code === 4409) status('opened in another tab');
      else status('disconnected (code ' + ev.code + ')');
    };
  }

  function fallback(form, error) {
    // The background session starts as before, and its page names this error (kn-53a20d3a)
    var f = document.createElement('input');
    f.type = 'hidden'; f.name = 'pty_error'; f.value = error;
    form.appendChild(f);
    HTMLFormElement.prototype.submit.call(form);
  }

  function start(form) {
    fetch(form.action + '/pty', {method: 'POST', body: new URLSearchParams(new FormData(form))})
      .then(function (r) {
        return r.json().catch(function () { return {error: 'HTTP ' + r.status}; })
          .then(function (body) { return {ok: r.ok, status: r.status, body: body}; });
      })
      .then(function (res) {
        if (!res.ok) return fallback(form, res.body.error || ('HTTP ' + res.status));
        var entry = {token: res.body.token, ws_url: res.body.ws_url,
                     session_name: res.body.session_name};
        store('entry', entry);
        // Same live session: the server already fed the new context, so just refocus
        if (current && current.token === entry.token && ws && ws.readyState <= WebSocket.OPEN) {
          current = entry;
          document.getElementById('help-name').textContent = entry.session_name;
          term.focus();
          return;
        }
        connect(entry);
      })
      .catch(function (e) { fallback(form, String(e)); });
  }

  forms.forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      againBtn.dataset.form = String(forms.indexOf(form));
      start(form);
    });
  });

  againBtn.addEventListener('click', function () {
    var form = forms[Number(againBtn.dataset.form || 0)] ||
               forms.filter(function (f) { return !f.elements.focus.value; })[0];
    if (form) start(form);
  });

  document.getElementById('help-close').addEventListener('click', function () {
    var entry = current || store('entry');
    store('entry', null);
    hide();
    if (!entry) return;
    var body = new URLSearchParams({token: entry.token});
    fetch(entry.ws_url.replace(/\/ws$/, '/close'), {method: 'POST', body: body})
      .catch(function () {});  // the idle timeout reaps it if this never lands
  });

  // Tab close and in-site navigation both fire pagehide; no beacon, the idle timeout reaps a closed tab
  var grip = document.getElementById('help-grip');
  grip.addEventListener('pointerdown', function (ev) {
    ev.preventDefault();
    grip.setPointerCapture(ev.pointerId);
    function move(e) { setWidth(window.innerWidth - e.clientX); }
    function up() {
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      store('width', clampWidth(panel.getBoundingClientRect().width));
      sendSize();
    }
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
  });

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { if (!panel.hidden) sendSize(); }, 100);
  });

  var saved = store('entry');
  if (saved && saved.token && saved.ws_url) connect(saved);
})();
