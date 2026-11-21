/* Settings menu: debug mode, open results in a new tab, results per page.
   Stored in localStorage ("stultus.settings"). The <head> snippet in
   _settings_head.html applies the debug class before first paint; this file does the rest. */
(function () {
  'use strict';

  var KEY = 'stultus.settings';
  var LIMITS = [10, 20, 30];
  var root = document.documentElement;

  function load() {
    var s = {};
    try { s = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) {}
    return {
      debug: !!s.debug,
      newTab: !!s.newTab,
      limit: LIMITS.indexOf(s.limit) > -1 ? s.limit : 10
    };
  }

  function save(s) {
    try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {}
    // The server reads this cookie to pick the page size when a /search URL has no ?limit=
    // (so it renders the right page on the first request instead of redirecting).
    try { document.cookie = 'stultus_limit=' + s.limit + '; Path=/; Max-Age=31536000; SameSite=Lax'; } catch (e) {}
  }

  var settings = load();

  // Dev server only (it injects window.__STULTUS_DEV__): force states for screenshots.
  var params = new URLSearchParams(location.search);
  var devOpen = false;
  if (window.__STULTUS_DEV__) {
    if (params.get('_debug') === '1') settings.debug = true;
    if (params.get('_newtab') === '1') settings.newTab = true;
    if (params.get('_perpage')) settings.limit = parseInt(params.get('_perpage'), 10) || 10;
    devOpen = params.get('_settings') === 'open';
  }

  function apply() {
    root.classList.toggle('debug', settings.debug);

    // open results in a new tab
    var links = document.querySelectorAll('#results-container .result a');
    for (var i = 0; i < links.length; i++) {
      if (settings.newTab) {
        links[i].target = '_blank';
        links[i].rel = 'noopener noreferrer';
      } else {
        links[i].removeAttribute('target');
        links[i].removeAttribute('rel');
      }
    }

    // results per page: search forms pass it along as ?limit=
    var forms = document.querySelectorAll('form[action="/search"]');
    for (var j = 0; j < forms.length; j++) {
      var input = forms[j].querySelector('input[name="limit"]');
      if (settings.limit !== 10) {
        if (!input) {
          input = document.createElement('input');
          input.type = 'hidden';
          input.name = 'limit';
          forms[j].appendChild(input);
        }
        input.value = settings.limit;
      } else if (input) {
        input.remove();
      }
    }
  }

  // ---- UI ----
  var GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="3"/>' +
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 ' +
    '1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06' +
    'a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06' +
    'a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0' +
    'a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09' +
    'a1.65 1.65 0 0 0-1.51 1z"/></svg>';

  var btn, panel, closeBtn;

  function build() {
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'settings-btn';
    btn.setAttribute('aria-label', 'Settings');
    btn.setAttribute('aria-haspopup', 'dialog');
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-controls', 'settings-panel');
    btn.title = 'Settings';
    btn.innerHTML = GEAR;

    panel = document.createElement('div');
    panel.id = 'settings-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'settings-title');
    panel.hidden = true;
    panel.innerHTML =
      '<h2 id="settings-title"><span>Settings</span>' +
      '<button type="button" id="settings-close" aria-label="Close settings">&times;</button></h2>' +
      '<div class="setting"><label for="set-debug">Debug mode' +
      '<small>Scores, query time, result count</small></label>' +
      '<input type="checkbox" id="set-debug"></div>' +
      '<div class="setting"><label for="set-newtab">Open results in a new tab</label>' +
      '<input type="checkbox" id="set-newtab"></div>' +
      '<div class="setting"><label for="set-limit">Results per page</label>' +
      '<select id="set-limit">' +
      LIMITS.map(function (n) { return '<option value="' + n + '">' + n + '</option>'; }).join('') +
      '</select></div>';

    document.body.appendChild(panel);
    document.body.appendChild(btn);
    closeBtn = panel.querySelector('#settings-close');

    var dbg = panel.querySelector('#set-debug');
    var tab = panel.querySelector('#set-newtab');
    var lim = panel.querySelector('#set-limit');
    dbg.checked = settings.debug;
    tab.checked = settings.newTab;
    lim.value = String(settings.limit);

    dbg.addEventListener('change', function () { settings.debug = dbg.checked; save(settings); apply(); });
    tab.addEventListener('change', function () { settings.newTab = tab.checked; save(settings); apply(); });
    lim.addEventListener('change', function () {
      settings.limit = parseInt(lim.value, 10);
      save(settings);
      apply();
      if (location.pathname === '/search') {   // re-run page 1 with the new page size
        var u = new URL(location.href);
        u.searchParams.set('limit', settings.limit);
        u.searchParams.delete('offset');
        location.assign(u.href);
      }
    });

    btn.addEventListener('click', function () { panel.hidden ? open() : close(); });
    closeBtn.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', function (e) {
      if (!panel.hidden && !panel.contains(e.target) && !btn.contains(e.target)) close();
    });
  }

  function focusables() {
    return panel.querySelectorAll('input, select, button');
  }

  function open() {
    panel.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    panel.querySelector('#set-debug').focus();
  }

  function close() {
    if (panel.hidden) return;
    panel.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    btn.focus();
  }

  function onKey(e) {
    if (panel.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key === 'Tab') {            // keep focus inside the dialog
      var f = focusables();
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      else if (!panel.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    }
  }

  // Warm the next results page when the pointer/finger/focus reaches the "Next" link, so the
  // click is usually served from the browser cache. Not done on page load: most visitors never
  // open page 2 and every prefetch is a real search on the server.
  function prefetchNext() {
    var next = document.getElementById('page-next');
    if (!next) return;
    var done = false;
    function go() {
      if (done) return;
      done = true;
      var l = document.createElement('link');
      l.rel = 'prefetch';
      l.href = next.href;
      document.head.appendChild(l);
    }
    ['mouseenter', 'touchstart', 'focus'].forEach(function (ev) {
      next.addEventListener(ev, go, { passive: true, once: true });
    });
  }

  function init() {
    // Only the search pages get the gear (creators and dashboard have their own layouts).
    if (document.getElementById('search-header')) build();
    apply();
    prefetchNext();
    if (devOpen && panel) open();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
