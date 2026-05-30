/* ============================================================
   library-shell.js
   ------------------------------------------------------------
   Sidebar toggle + iOS body lock + cross-page nav + common UI
   helpers shared by list-to-detail admin pages (dishes.html,
   users.html, recipe_form_styled.html, future cookbooks.html,
   etc.). Sits beside library-shell.css.

   Usage in a page:

       <script src="/forms/library-shell.js"></script>
       <script>
         LibraryShell.init({
           sidebarSelector:        '#sidebar',
           sidebarToggleSelector:  '#sidebarToggle',
         });
         LibraryShell.initNav({ currentPage: 'dishes' });
       </script>

   init() wires the left sidebar (toggle, click-outside-close, iOS body
   lock). initNav() injects the right-side cross-page nav menu (⋮ icon,
   dropdown of the other admin pages, coming-soon overlay for no-ops).
   Both are safe to call independently — a page with no sidebar can
   call initNav() only.

   Helpers exposed:
     LibraryShell.openSidebar()
     LibraryShell.closeSidebar()
     LibraryShell.toggleSidebar()
     LibraryShell.isNarrow()         // window.matchMedia('(max-width:760px)')
     LibraryShell.closeOnNarrow()    // close sidebar only if narrow viewport
     LibraryShell.escapeHtml(s)
     LibraryShell.fmtDate(iso)       // relative ("3 hr ago") fallback to absolute
     LibraryShell.NAV_ITEMS          // editable nav-items array (see below)
   ============================================================ */

(function () {
  const state = {
    sidebar: null,
    sidebarToggle: null,
  };

  // === X-Self-User-Id header auto-attach ===
  // Every page that loads library-shell.js (recipe form, dishes,
  // users, install) makes API calls that the server uses to check
  // permissions (gates on master writes, /auth/me identity, etc.).
  // Read app:self_user_id once from localStorage and stamp it on every
  // outbound fetch so callers don't have to thread it manually. The
  // legacy sidebar:user_id key is honored as fallback for sessions
  // pre-dating the picker login (2026-05-21). Pre-Ghost this is a
  // trust-the-client header — fine for a private app — and on Ghost
  // integration the server-side validator swaps to a session JWT.
  (function patchFetch() {
    if (window.__bccFetchPatched) return;
    window.__bccFetchPatched = true;
    const _origFetch = window.fetch.bind(window);
    function selfUid() {
      try {
        const explicit = localStorage.getItem('app:self_user_id');
        if (explicit && parseInt(explicit, 10) > 0) return String(parseInt(explicit, 10));
        const legacy = localStorage.getItem('sidebar:user_id');
        if (legacy && parseInt(legacy, 10) > 0) return String(parseInt(legacy, 10));
      } catch (e) { /* private mode / no storage */ }
      return null;
    }
    window.fetch = function (input, init) {
      const uid = selfUid();
      if (uid) {
        init = init ? Object.assign({}, init) : {};
        const h = new Headers(init.headers || {});
        if (!h.has('X-Self-User-Id')) h.set('X-Self-User-Id', uid);
        init.headers = h;
      }
      return _origFetch(input, init);
    };
  })();

  function openSidebar() {
    if (!state.sidebar) return;
    state.sidebar.classList.add('open');
    document.body.classList.add('sidebar-open');
  }
  function closeSidebar() {
    if (!state.sidebar) return;
    state.sidebar.classList.remove('open');
    document.body.classList.remove('sidebar-open');
  }
  function toggleSidebar() {
    if (!state.sidebar) return;
    if (state.sidebar.classList.contains('open')) closeSidebar();
    else openSidebar();
  }
  function isNarrow() {
    return window.matchMedia('(max-width: 760px)').matches;
  }
  function closeOnNarrow() {
    if (isNarrow()) closeSidebar();
  }

  // Transient confirmation toast — universal across template children.
  let _flashTimer = null;
  function flash(message, isError) {
    if (!document.getElementById('ls-flash-style')) {
      const st = document.createElement('style');
      st.id = 'ls-flash-style';
      st.textContent =
        '#ls-flash{position:fixed;left:50%;bottom:30px;' +
        'transform:translateX(-50%) translateY(8px);background:var(--ink,#1d1d1f);' +
        'color:#fff;padding:11px 20px;border-radius:9px;font:inherit;font-size:.92em;' +
        'font-weight:400;letter-spacing:-.003em;box-shadow:0 6px 24px rgba(40,30,20,.22);' +
        'z-index:1000;opacity:0;pointer-events:none;transition:opacity .18s ease,transform .18s ease;' +
        'max-width:80vw;}#ls-flash.show{opacity:.97;transform:translateX(-50%) translateY(0);}' +
        '#ls-flash.err{background:#a3382b;}';
      document.head.appendChild(st);
    }
    let el = document.getElementById('ls-flash');
    if (!el) { el = document.createElement('div'); el.id = 'ls-flash'; document.body.appendChild(el); }
    el.textContent = message || 'Saved';
    el.className = isError ? 'show err' : 'show';
    if (_flashTimer) clearTimeout(_flashTimer);
    _flashTimer = setTimeout(() => { el.className = ''; }, isError ? 4000 : 1800);
  }

  // Universal post-save flow for template children: flash a confirmation,
  // let the page clear its own form (onClear), then return to the sidebar.
  function afterSave(opts) {
    opts = opts || {};
    flash(opts.message || 'Saved', false);
    if (typeof opts.onClear === 'function') {
      try { opts.onClear(); } catch (e) { console.warn('[LibraryShell] afterSave onClear failed', e); }
    }
    if (opts.returnToSidebar !== false) openSidebar();
  }

  // App-shell header brand: site name + optional logo, linking home.
  // Config-driven via GET /branding (bcc_config.json) with the BRAND
  // const as a synchronous fallback so the header never flashes empty.
  function applyBranding(brandEl, opts) {
    opts = opts || {};
    if (!brandEl) return;
    brandEl.textContent = opts.brand || BRAND;   // immediate fallback
    if (!document.getElementById('ls-brand-style')) {
      const st = document.createElement('style');
      st.id = 'ls-brand-style';
      st.textContent =
        '.app-header h1 .brand-link{display:inline-flex;align-items:center;gap:9px;' +
        'color:inherit;text-decoration:none;}' +
        '.app-header h1 .brand-link:hover .brand-name{text-decoration:underline;}' +
        '.app-header h1 .brand-logo{height:1.3em;width:auto;display:block;}';
      document.head.appendChild(st);
    }
    window.fetch('/branding').then(r => r.ok ? r.json() : null).then(b => {
      if (!b) return;
      const name = opts.brand || b.name || BRAND;
      const a = document.createElement('a');
      a.className = 'brand-link';
      a.href = b.home_url || '#';
      if (b.logo_url) {
        const img = document.createElement('img');
        img.className = 'brand-logo'; img.src = b.logo_url; img.alt = name;
        a.appendChild(img);
      }
      const span = document.createElement('span');
      span.className = 'brand-name'; span.textContent = name;
      a.appendChild(span);
      brandEl.innerHTML = '';
      brandEl.appendChild(a);
    }).catch(() => {});
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
  }

  // === Exceptionalism grade badge ===
  // Returns HTML string for a tier-keyed monogram badge. Pair with the
  // .exc-badge CSS in recipe_form_styled.html (also imported by
  // dishes.html via the shared stylesheet pipeline).
  //
  // Usage:
  //   const html = renderExcBadge({grade: 'A-', score: 88.3, basis: {...}});
  //   const html = renderExcBadge(exc, {size: 'small'});  // sidebar
  //   const html = renderExcBadge(exc, {size: 'large', includeScore: true});
  //
  // Returns '' when exc is null/missing — callers can ${...} this directly
  // into a template without a guard.
  function gradeToTier(grade) {
    if (!grade) return 'tier-none';
    if (grade === 'A+') return 'tier-a-plus';
    if (grade === 'A')  return 'tier-a';
    if (grade === 'A-') return 'tier-a-minus';
    if (grade.startsWith('B')) return 'tier-b';
    if (grade.startsWith('C')) return 'tier-c';
    if (grade.startsWith('D')) return 'tier-d';
    if (grade === 'F') return 'tier-f';
    return 'tier-none';
  }

  function renderExcBadge(exc, opts) {
    if (!exc || !exc.grade) return '';
    opts = opts || {};
    const size = opts.size || 'medium';
    const tier = gradeToTier(exc.grade);
    const letter = exc.grade[0];
    const suffix = exc.grade.length > 1 ? exc.grade.slice(1) : '';
    const score = (typeof exc.score === 'number') ? exc.score.toFixed(1) : '';
    const basis = exc.basis || {};
    const basisParts = [];
    if (basis.model) basisParts.push(basis.model);
    if (typeof basis.n === 'number') basisParts.push('n=' + basis.n);
    if (typeof basis.sigma_effective === 'number') {
      basisParts.push('σ=' + basis.sigma_effective.toFixed(2));
    }
    const basisStr = basisParts.length ? '  ·  ' + basisParts.join(', ') : '';
    const tooltip = 'Exceptionalism ' + exc.grade
      + (score ? '  ·  score ' + score : '')
      + basisStr;
    const suffixHtml = suffix
      ? '<span class="exc-suffix">' + escapeHtml(suffix) + '</span>'
      : '';
    return '<span class="exc-badge ' + size + ' ' + tier + '" '
      + 'title="' + escapeHtml(tooltip) + '" '
      + 'aria-label="' + escapeHtml(tooltip) + '">'
      + '<span class="exc-letter">' + escapeHtml(letter) + '</span>'
      + suffixHtml
      + '</span>';
  }

  function fmtDate(s) {
    if (!s) return '—';
    try {
      const d = new Date(s);
      if (Number.isNaN(d.getTime())) return s;
      const ageMs = Date.now() - d.getTime();
      const ageHrs = ageMs / 3600000;
      if (ageHrs < 1) return Math.round(ageMs / 60000) + ' min ago';
      if (ageHrs < 24) return Math.round(ageHrs) + ' hr ago';
      if (ageHrs < 24 * 7) return Math.round(ageHrs / 24) + ' d ago';
      return d.toLocaleDateString();
    } catch (e) {
      return s;
    }
  }

  // === Identity badge in title bar ===
  // Text-only "signed in as" chip. Reference patterns: GitHub's
  // top-right account pill, Linear's workspace switcher, Notion's
  // account row. Common thread: italic editor-page name with a subtle
  // directional indicator, NO avatar circle (looks out of place at
  // form-page scale and absurd on phones), NO link underline. Reads
  // as "your byline" not "a button."
  //
  // Why this matters: every API call dispatches by X-Self-User-Id
  // (master writes, sidebar load, claim flow). Easy to think you're
  // acting as User A when you're actually User B — silent data scope
  // bugs. Putting the name in the header makes identity unambiguous.
  //
  // The right-up arrow appears on hover (or always on mobile, where
  // there's no hover state) — that's the click affordance.
  function initIdentityBadge() {
    const headerInner = document.querySelector('.app-header .header-inner');
    if (!headerInner) return;
    if (headerInner.querySelector('.identity-badge')) return;  // idempotent

    const badge = document.createElement('a');
    badge.className = 'identity-badge';
    badge.href = '/forms/users.html';
    badge.title = 'Click to switch user';
    badge.innerHTML =
      '<span class="identity-name muted">…</span>' +
      '<span class="identity-arrow">↗</span>';

    // Sit to the RIGHT, adjacent to the nav toggle. The .nav-spacer
    // (flex:1) sits between the title and the badge, so the badge
    // floats next to the ⋮ menu rather than next to the page title.
    // Insert AFTER the spacer (i.e. before whatever comes after — the
    // nav-toggle if it's already mounted, otherwise just append).
    const navSpacer = headerInner.querySelector('.nav-spacer');
    if (navSpacer && navSpacer.nextSibling) {
      headerInner.insertBefore(badge, navSpacer.nextSibling);
    } else if (navSpacer) {
      // Spacer exists, nothing after it yet — append (initNav will
      // add the ⋮ toggle after the badge in a moment).
      headerInner.appendChild(badge);
    } else {
      // No spacer yet (initNav hasn't run, or this page doesn't use it).
      // Just append; the spacer will be inserted before us by initNav.
      headerInner.appendChild(badge);
    }

    // Hydrate via /auth/me. patchFetch already attaches X-Self-User-Id.
    window.fetch('/auth/me')
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        const u = data && data.user;
        if (!u) {
          badge.innerHTML =
            '<span class="identity-name muted">not signed in</span>' +
            '<span class="identity-arrow">↗</span>';
          badge.title = 'Click to pick a user';
          return;
        }
        const nm = (u.name || u.email || '').trim();
        const uid = u.user_id;
        const role = (data.role || 'member');
        const display = nm || ('user ' + uid);
        badge.innerHTML =
          '<span class="identity-name">' + escapeHtml(display) + '</span>' +
          '<span class="identity-arrow">↗</span>';
        // Tooltip carries the precise lookup data so the at-a-glance
        // chip stays clean while audit info is one hover away.
        badge.title = display +
                      '  ·  user_id ' + uid +
                      '  ·  role ' + role +
                      '\nClick to switch.';
      })
      .catch(() => {
        badge.innerHTML =
          '<span class="identity-name muted">unknown</span>' +
          '<span class="identity-arrow">↗</span>';
      });
  }

  const BRAND = 'Best Cooks Club';   // synchronous fallback; /branding is source of truth

  function init(opts) {
    opts = opts || {};
    state.sidebar = document.querySelector(opts.sidebarSelector || '#sidebar');
    state.sidebarToggle = document.querySelector(opts.sidebarToggleSelector || '#sidebarToggle');
    if (!state.sidebar || !state.sidebarToggle) {
      console.warn('[LibraryShell] sidebar or toggle element not found; skipping wiring');
      return;
    }
    state.sidebarToggle.addEventListener('click', toggleSidebar);
    // Click anywhere outside the sidebar (and not the toggle itself)
    // closes it when open. Keeps the open-sidebar surface clean.
    document.addEventListener('click', (e) => {
      if (!state.sidebar.classList.contains('open')) return;
      if (state.sidebar.contains(e.target)) return;
      if (state.sidebarToggle.contains(e.target)) return;
      if (e.target.closest && e.target.closest('.sidebar-opener')) return;
      closeSidebar();
    });

    // The list toggle belongs WITH the list, not the page brand bar:
    // move it into the sidebar's own header (next to the list title), so
    // the header slot it vacated is free for the logo/brand. Inside the
    // sidebar it can only CLOSE the list; the floating opener reopens it.
    const listHeader = state.sidebar.querySelector('h2');
    if (listHeader && state.sidebarToggle.parentElement !== listHeader) {
      state.sidebarToggle.classList.add('in-list-header');
      listHeader.insertBefore(state.sidebarToggle, listHeader.firstChild);
    }
    if (!document.querySelector('.sidebar-opener')) {
      const opener = document.createElement('button');
      opener.type = 'button';
      opener.className = 'sidebar-opener';
      opener.setAttribute('aria-label', 'Open list');
      opener.textContent = '☰';
      opener.addEventListener('click', (e) => { e.stopPropagation(); openSidebar(); });
      document.body.appendChild(opener);
    }
    // Identity badge mounting is handled by initNav() — that's the
    // right-hand chrome and it runs AFTER it inserts the nav-spacer,
    // so the badge lands on the right (next to the ⋮ menu). Mounting
    // here in init() (which runs first on dishes/users/install) would
    // mount BEFORE the spacer exists, parking the badge on the LEFT
    // side of the header — that's the bug the user flagged
    // 2026-05-28 ("user id at top needs to be on the right on all
    // pages, not just recipes"). Idempotent so this isn't a regression
    // for the recipe form (which only calls initNav).

    // Top line is the site brand (config-driven via GET /branding), not
    // the section name (the section lives in the sidebar h2 + active nav
    // row). Universal across template children; override with
    // init({ brand: '…' }).
    applyBranding(document.querySelector('.app-header h1'), opts);

    // Sidebar visible at startup for template children (the list is the
    // landing surface). Opt out with init({ sidebarStartOpen: false }).
    if (opts.sidebarStartOpen !== false) openSidebar();
  }

  // ============================================================
  //  Cross-page nav (right-side ⋮ menu)
  // ============================================================

  // Single source of truth for the admin nav. Adding a new entity page
  // is a one-line addition here — every page that calls initNav() gets
  // the new item automatically. `page` is the identifier callers pass
  // to initNav({currentPage}) to mark this row .active.
  // `comingSoon: true` means clicking the row opens the coming-soon
  // overlay instead of navigating; promote to a real href when the
  // page actually exists.
  const NAV_ITEMS = [
    { page: 'recipes',   label: 'Recipes',   href: '/forms/recipe_form_styled.html' },
    { page: 'dishes',    label: 'Dishes',    href: '/forms/dishes.html' },
    { page: 'chapters',  label: 'Chapters',  href: '/forms/chapters.html' },
    { page: 'users',     label: 'Users',     href: '/forms/users.html' },
    { page: 'cookbooks', label: 'Cookbooks', comingSoon: true },
    { page: 'equipment', label: 'Equipment', comingSoon: true },
    { page: 'gourmet',   label: 'Gourmet',   comingSoon: true },
    // Utility / setup items sit at the bottom, separated from the
    // entity pages above. "Install bookmarklet" is the most-needed
    // utility today; future items (settings, exports, etc.) go here.
    // `action` items run JS instead of navigating (see initNav wiring).
    { page: 'run-jobs',  label: 'Run queued jobs', action: 'runQueuedJobs' },
    { page: 'install',   label: 'Install bookmarklet', href: '/forms/install.html' },
  ];

  function showComingSoon(label) {
    // Take-over overlay (dimmer + centered card). Backdrop click or
    // OK button dismisses. Esc also dismisses.
    const overlay = document.createElement('div');
    overlay.className = 'coming-soon-overlay';
    overlay.innerHTML =
      '<div class="coming-soon-card">' +
        '<h2>' + escapeHtml(label) + '</h2>' +
        '<p>Coming soon. This page hasn’t been built yet — it’s on the roadmap.</p>' +
        '<button type="button">OK</button>' +
      '</div>';
    const dismiss = () => {
      document.removeEventListener('keydown', onKey);
      overlay.remove();
    };
    const onKey = (e) => { if (e.key === 'Escape') dismiss(); };
    overlay.addEventListener('click', (e) => {
      // Card click stays inside; only backdrop click dismisses.
      if (e.target === overlay) dismiss();
    });
    overlay.querySelector('button').addEventListener('click', dismiss);
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
  }

  // ============================================================
  //  Queued-jobs drain (nav action)
  // ============================================================
  //
  // The server's background poll runner is disabled on purpose, so
  // enqueued jobs (dish refreshes, etc.) sit in 'queued' until something
  // dispatches them. POST /jobs/run-queued kicks off a single server-side
  // background drain and returns the ordered job-id list; we watch each
  // job's SSE stream in turn and tail its log into an overlay. Available
  // from every page via the ⋮ menu.

  // How many jobs are currently queued? Drives the count badge on the
  // "Run queued jobs" menu row. Resolves to 0 on any error (badge hides).
  function queuedJobCount() {
    return window.fetch('/jobs?status=queued&limit=100')
      .then(r => r.ok ? r.json() : [])
      .then(rows => Array.isArray(rows) ? rows.length : 0)
      .catch(() => 0);
  }

  let _jobsOverlay = null;
  function _ensureJobsOverlay() {
    if (_jobsOverlay) return _jobsOverlay;
    const overlay = document.createElement('div');
    overlay.className = 'coming-soon-overlay';  // reuse dimmer + centering
    overlay.innerHTML =
      '<div class="coming-soon-card" style="max-width:640px;width:90vw;text-align:left">' +
        '<h2 style="margin-top:0">Run queued jobs</h2>' +
        '<p class="jobs-runner-status" style="margin:0 0 8px"></p>' +
        '<pre class="jobs-runner-log" style="background:#0e0e0e;color:#cdd6cd;' +
          'font:12px/1.45 ui-monospace,Menlo,monospace;padding:10px 12px;' +
          'border-radius:8px;max-height:48vh;overflow:auto;white-space:pre-wrap;' +
          'margin:0 0 12px;display:none"></pre>' +
        '<div style="text-align:right">' +
          '<button type="button" class="jobs-runner-close">Close</button>' +
        '</div>' +
      '</div>';
    const close = () => { overlay.remove(); _jobsOverlay = null; };
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('.jobs-runner-close').addEventListener('click', close);
    document.body.appendChild(overlay);
    _jobsOverlay = overlay;
    return overlay;
  }

  function _setJobsStatus(text) {
    if (!_jobsOverlay) return;
    _jobsOverlay.querySelector('.jobs-runner-status').textContent = text;
  }
  function _appendJobsLog(line) {
    if (!_jobsOverlay) return;
    const pre = _jobsOverlay.querySelector('.jobs-runner-log');
    pre.style.display = 'block';
    pre.textContent += (pre.textContent ? '\n' : '') + line;
    pre.scrollTop = pre.scrollHeight;
  }

  // Watch one job's SSE stream to completion. Resolves with the final
  // status string ('success' | 'error' | 'cancelled').
  function _watchJob(jobId, idx, total) {
    return new Promise((resolve) => {
      const stream = new EventSource('/jobs/' + jobId + '/stream');
      stream.addEventListener('status', (e) => {
        try {
          const d = JSON.parse(e.data);
          _setJobsStatus('Job #' + jobId + ' (' + idx + '/' + total + '): ' + d.status + '…');
        } catch (_) { /* ignore */ }
      });
      stream.addEventListener('log', (e) => {
        try { _appendJobsLog(JSON.parse(e.data).line); } catch (_) { /* ignore */ }
      });
      stream.addEventListener('done', (e) => {
        let status = 'done';
        try { status = JSON.parse(e.data).status; } catch (_) { /* ignore */ }
        stream.close();
        resolve(status);
      });
      stream.addEventListener('error', () => {
        // Transient tunnel/network blip — EventSource auto-reconnects.
        // If the job already finished, the next poll yields `done`.
      });
    });
  }

  let _draining = false;
  function runQueuedJobs() {
    if (_draining) { _ensureJobsOverlay(); return; }
    _draining = true;
    const overlay = _ensureJobsOverlay();
    _setJobsStatus('Starting…');
    window.fetch('/jobs/run-queued', { method: 'POST' })
      .then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (res.status === 403) {
          _setJobsStatus(data.detail || 'You don’t have permission to run jobs.');
          return;
        }
        if (res.status === 409) {
          _setJobsStatus('A drain is already running' +
            (data.running && data.running.length ? ' (job #' + data.running[0] + ').' : '.'));
          return;
        }
        if (!res.ok) { _setJobsStatus('Failed to start: HTTP ' + res.status); return; }
        const ids = data.job_ids || [];
        if (!ids.length) { _setJobsStatus(data.message || 'No queued jobs.'); return; }
        let ok = 0, bad = 0;
        for (let i = 0; i < ids.length; i++) {
          const status = await _watchJob(ids[i], i + 1, ids.length);
          if (status === 'success') ok++; else bad++;
        }
        _setJobsStatus('Done — ' + ok + ' succeeded' + (bad ? ', ' + bad + ' failed' : '') + '.');
        _refreshJobBadges();  // queued count is now 0
      })
      .catch((err) => { _setJobsStatus('Error: ' + err); })
      .finally(() => { _draining = false; });
  }

  // Action registry — nav items with `action: '<key>'` dispatch here.
  const NAV_ACTIONS = {
    runQueuedJobs: runQueuedJobs,
  };

  // Update every mounted "Run queued jobs" row's count badge.
  function _refreshJobBadges() {
    const rows = document.querySelectorAll('.nav-item[data-page="run-jobs"]');
    if (!rows.length) return;
    queuedJobCount().then(n => {
      rows.forEach(row => {
        let badge = row.querySelector('.badge-count');
        if (n > 0) {
          if (!badge) {
            badge = document.createElement('span');
            badge.className = 'badge-soon badge-count';  // reuse pill styling
            row.appendChild(badge);
          }
          badge.textContent = String(n);
        } else if (badge) {
          badge.remove();
        }
      });
    });
  }

  function initNav(opts) {
    opts = opts || {};
    const currentPage = opts.currentPage || '';
    const items = opts.items || NAV_ITEMS;

    // Where to mount the toggle button:
    //   - If the page uses the library-shell .header-inner, append the
    //     toggle there (header is fixed, content is centered).
    //   - Otherwise, mount the toggle as a fixed top-right button.
    const headerInner = document.querySelector('.app-header .header-inner');

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'nav-toggle';
    toggle.setAttribute('aria-label', 'Open page navigation');
    toggle.innerHTML = '⋮';

    if (headerInner) {
      // Push the toggle to the right edge with a spacer if the header
      // doesn't already have one. The spacer ate `flex: 1` so any title
      // on the left stays left and the toggle sits hard right.
      if (!headerInner.querySelector('.nav-spacer')) {
        const spacer = document.createElement('div');
        spacer.className = 'nav-spacer';
        headerInner.appendChild(spacer);
      }
      // Identity badge sits between the spacer (title-side) and the
      // nav toggle. Idempotent — pages that ALSO call init() won't
      // get a duplicate. Mounted from initNav too because the recipe
      // form doesn't call init() (it has its own sidebar logic).
      initIdentityBadge();
      headerInner.appendChild(toggle);
    } else {
      toggle.style.position = 'fixed';
      toggle.style.top = '14px';
      toggle.style.right = '16px';
      toggle.style.zIndex = '101';
      document.body.appendChild(toggle);
    }

    // Dropdown markup (mounted on body so its absolute positioning is
    // viewport-relative regardless of any transformed ancestor).
    const menu = document.createElement('div');
    menu.className = 'nav-menu';
    menu.innerHTML = items.map(item => {
      const isActive = item.page === currentPage;
      const cls = 'nav-item' + (isActive ? ' active' : '');
      const isButton = item.comingSoon || item.action;
      const tag = isButton ? 'button' : 'a';
      const attrs = isButton
        ? `type="button" data-page="${escapeHtml(item.page)}"`
        : `href="${escapeHtml(item.href)}" data-page="${escapeHtml(item.page)}"`;
      const badge = item.comingSoon ? '<span class="badge-soon">soon</span>' : '';
      return `<${tag} class="${cls}" ${attrs}>${escapeHtml(item.label)}${badge}</${tag}>`;
    }).join('');
    document.body.appendChild(menu);
    _refreshJobBadges();  // initial queued count on page load

    function closeMenu() { menu.classList.remove('open'); }
    function openMenu() { menu.classList.add('open'); }

    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      if (menu.classList.contains('open')) { closeMenu(); return; }
      _refreshJobBadges();  // keep the queued count fresh each open
      openMenu();
    });

    // Click outside the menu (and outside the toggle) closes it.
    document.addEventListener('click', (e) => {
      if (!menu.classList.contains('open')) return;
      if (menu.contains(e.target)) return;
      if (toggle.contains(e.target)) return;
      closeMenu();
    });

    // Wire each item: real links navigate normally; coming-soon items
    // intercept and show the overlay.
    menu.querySelectorAll('.nav-item').forEach(el => {
      const page = el.getAttribute('data-page');
      const cfg = items.find(it => it.page === page);
      if (cfg && cfg.action) {
        el.addEventListener('click', (e) => {
          e.preventDefault();
          closeMenu();
          const fn = NAV_ACTIONS[cfg.action];
          if (fn) fn();
        });
      } else if (cfg && cfg.comingSoon) {
        el.addEventListener('click', (e) => {
          e.preventDefault();
          closeMenu();
          showComingSoon(cfg.label);
        });
      } else {
        // Active row = current page; clicking does nothing but close.
        if (page === currentPage) {
          el.addEventListener('click', (e) => {
            e.preventDefault();
            closeMenu();
          });
        }
      }
    });
  }

  window.LibraryShell = {
    init,
    initNav,
    initIdentityBadge,
    openSidebar,
    closeSidebar,
    toggleSidebar,
    isNarrow,
    closeOnNarrow,
    flash,
    afterSave,
    applyBranding,
    escapeHtml,
    fmtDate,
    renderExcBadge,
    gradeToTier,
    runQueuedJobs,
    queuedJobCount,
    NAV_ITEMS,
    BRAND,
  };
})();
