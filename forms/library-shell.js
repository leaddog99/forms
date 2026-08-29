/* ============================================================
   library-shell.js
   ------------------------------------------------------------
   Sidebar toggle + iOS body lock + cross-page nav + common UI
   helpers shared by list-to-detail admin pages (dishes.html,
   users.html, recipe_form_styled.html, future cookbooks.html,
   etc.). Sits beside library-shell.css.

   ------------------------------------------------------------
   EVERY NEW PAGE STARTS FROM THIS CONTRACT. Four lines and one
   block; there is no opt-out, and a page that skips it does not
   look like the rest of the system:

       <style> …page-specific rules only… </style>
       <link rel="stylesheet" href="/forms/library-shell.css">
       <link rel="stylesheet" href="/forms/tokens.css">   <-- LAST
       <script src="/forms/library-shell.js"></script>
     </head>
     <body>
       <header class="app-header">
         <div class="header-inner"><h1></h1></div>
       </header>

   and call LibraryShell.initNav({ currentPage: '…' }) — which
   fills that <h1> with the brand and mounts the identity badge
   plus both burgers (nav, who you are, unlock admin, sign out).
   Call it UNCONDITIONALLY: if your page returns early when signed
   out, brand the header yourself first or it renders as an empty
   bar.

   Do NOT define --accent/--bg/--ink/… in the page. tokens.css is
   the single palette and is loaded last so it wins; a page-local
   :root is how eight pages ended up with three different accents,
   two of them claiming to be the same clay. Page-specific tokens
   that tokens.css does not own (--warn/--ok/--info) are fine.

   Do NOT paste .nav-toggle / .nav-menu / .coming-soon-* rules in.
   library-shell.css has them; copies drift (there were four
   versions across five pages).
   ------------------------------------------------------------

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
     LibraryShell.hostOf(url)        // display hostname: no scheme/www./path
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
        // user_id 0 is the master/curator identity (saves to master,
        // unlocks admin) — an EXPLICIT 0 is a real login and must be sent,
        // so accept >= 0 here. A missing/blank/invalid value still yields
        // no header (anonymous), never 0.
        const explicit = localStorage.getItem('app:self_user_id');
        if (explicit != null && explicit !== '') {
          const n = parseInt(explicit, 10);
          if (Number.isInteger(n) && n >= 0) return String(n);
        }
        // Legacy fallback only covers positive ids (master was never stored here).
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
        // The curator token, sent for EVERY uid that has one — not just uid 0.
        // The header alone no longer grants owner (it used to, which on a public
        // hostname was a full admin bypass), so a staff role needs this token to
        // resolve as itself. Minted by POST /auth/master.
        //
        // It was gated on uid === '0', which silently broke "unlock admin" for
        // every account except Master: the password was accepted, the token was
        // stored, the page reloaded — and the token was never sent, so
        // _resolve_caller saw none, kept clamping the role to 'member', and
        // re-rendered the unlock link. Nothing reported an error because nothing
        // had failed; the credential just never left the browser. The token is
        // NOT bound to a user_id (see unlockAdmin), and the server ignores it for
        // an account whose role is already 'member', so sending it always is both
        // necessary and safe.
        if (!h.has('X-Master-Token')) {
          try {
            const t = localStorage.getItem('app:master_token');
            if (t) h.set('X-Master-Token', t);
          } catch (e) { /* private mode */ }
        }
        // Accounts WITH a password are no longer resolvable from the id header
        // alone — they need the session token minted by POST /auth/login. Sent
        // for every uid; the server ignores it where a password isn't set, which
        // is what lets accounts harden one at a time.
        if (!h.has('X-Session-Token')) {
          try {
            const st = localStorage.getItem('app:session_token');
            if (st) h.set('X-Session-Token', st);
          } catch (e) { /* private mode */ }
        }
        init.headers = h;
      }
      // A 401 means the session lapsed — the token expired, or it was cleared.
      // Say so once, plainly, instead of letting each caller surface a generic
      // failure. Previously this arrived as a permissions 403 that claimed an
      // owner account's role was 'anonymous', which reads as a contradiction
      // rather than "sign in again".
      return _origFetch(input, init).then(function (resp) {
        if (resp && resp.status === 401) _sessionLapsed();
        return resp;
      });
    };
  })();

  // Shown at most once per page — a lapsed session usually fails several
  // in-flight requests at once, and three identical banners is noise.
  let _lapsedShown = false;
  function _sessionLapsed() {
    if (_lapsedShown) return;
    _lapsedShown = true;
    try { localStorage.removeItem('app:master_token'); } catch (e) { /* private mode */ }
    const bar = document.createElement('div');
    bar.setAttribute('role', 'alert');
    bar.style.cssText =
      'position:fixed;left:0;right:0;top:0;z-index:1200;background:#a3382b;color:#fff;' +
      'padding:10px 16px;font:inherit;font-size:.9em;display:flex;gap:14px;' +
      'align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.2)';
    bar.innerHTML =
      '<span>Your session has expired — you are signed out.</span>' +
      '<a href="/forms/users.html" style="color:#fff;font-weight:600">Sign in again ↗</a>' +
      '<button type="button" style="background:none;border:1px solid rgba(255,255,255,.6);' +
      'color:#fff;border-radius:6px;padding:2px 9px;cursor:pointer">Dismiss</button>';
    bar.querySelector('button').addEventListener('click', () => bar.remove());
    (document.body || document.documentElement).appendChild(bar);
  }

  function openSidebar() {
    if (!state.sidebar) return;
    state.sidebar.classList.add('open');
    document.body.classList.add('sidebar-open');
    // The menu lives at the top of the document, so on a scrolled-down page
    // it opens above the fold. Auto-scroll to the top so it's visible without
    // the user manually scrolling up.
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); }
    catch (_) { window.scrollTo(0, 0); }
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

  // Display hostname of a URL: no scheme, no www., no path. THE one hostname
  // derivation — pages had grown two hand-rolled variants (one kept www., so
  // the same site displayed differently between panels on one page). Falls
  // back to string-stripping for non-URL input (a bare domain, a fragment).
  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); }
    catch (_) {
      return String(url == null ? '' : url)
        .replace(/^https?:\/\//, '').replace(/^www\./, '').split('/')[0];
    }
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
      // SQLite's datetime('now') yields "YYYY-MM-DD HH:MM:SS" — UTC, but with
      // no marker saying so, and JS parses that shape as LOCAL time. On EDT that
      // put a just-written timestamp four hours in the FUTURE, and since the
      // "< 1 hour" branch is also true for negatives it rendered as "-237 min
      // ago". Treat a bare space-separated stamp as the UTC it actually is.
      const iso = (typeof s === 'string' && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(s.trim()))
        ? s.trim().replace(' ', 'T') + 'Z'
        : s;
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return s;
      const ageMs = Date.now() - d.getTime();
      const ageHrs = ageMs / 3600000;
      // Clock skew (or a stamp written a moment ago by a server a second ahead)
      // should read as "just now", never as a negative age.
      if (ageMs < 0) return ageMs > -120000 ? 'just now' : d.toLocaleString();
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
  // === Sign out / lock admin =================================================
  // There was a way in (the Master password) and no way out: the only exit was
  // switching to another user or clearing localStorage by hand. Two separate
  // actions, because they answer different questions:
  //
  //   lock admin — discard the curator token, KEEP your identity. You stay
  //                yourself with your own recipes; staff permissions drop. This
  //                is the one to use on a shared screen.
  //   sign out   — discard everything, back to the picker.
  //
  // The curator token is a stateless HMAC, so "discard" is client-side only: it
  // is gone from THIS browser, but a token copied elsewhere stays valid until it
  // expires (12h). Real revocation needs a server-side blocklist. Acceptable
  // while the token can only be obtained by knowing the password — worth
  // revisiting if tokens ever get longer-lived.
  function clearMasterToken() {
    try { localStorage.removeItem('app:master_token'); } catch (e) { /* private mode */ }
  }

  function signOut() {
    try {
      localStorage.removeItem('app:self_user_id');
      localStorage.removeItem('sidebar:user_id');
      localStorage.removeItem('app:master_token');
      localStorage.removeItem('app:session_token');
    } catch (e) { /* private mode */ }
    // The front door, not the user editor. This used to send everyone to
    // /forms/users.html, which is an ADMIN page and 404s on the customer host —
    // so signing out of bestcooksclub.com landed on a not-found page. `/` is the
    // right destination on both hosts now that it exists: it serves whichever
    // home the hostname calls for, each with its own sign-in form.
    window.location.href = '/';
  }

  // Unlock staff permissions on the CURRENT identity — no user switch. The token
  // proves you know the curator password; it is not bound to a user_id, so the
  // same one that makes uid 0 owner also unlocks a staff row's real role.
  // Resolves TRUE once the master token is stored, FALSE on any rejection, so a
  // caller can await it and branch. `opts.reload === false` suppresses the page
  // reload for in-flight work that must survive the unlock (the bookmarklet
  // staged-import retry) — the header's inline link still reloads, since there
  // the whole point is to re-render the page as a curator.
  function unlockAdmin(password, onError, opts) {
    const doReload = !(opts && opts.reload === false);
    return window.fetch('/auth/master', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password })
    }).then(function (r) {
      if (!r.ok) {
        onError(r.status === 429 ? 'Too many attempts — wait 15 minutes.'
              : r.status === 503 ? 'No master password is configured on this instance.'
              : 'Incorrect password.');
        return null;
      }
      return r.json();
    }).then(function (d) {
      if (!d || !d.token) return false;
      try { localStorage.setItem('app:master_token', d.token); } catch (e) { /* private mode */ }
      if (doReload) window.location.reload();
      return true;
    }).catch(function (e) { onError('Could not reach the server: ' + e.message); return false; });
  }

  // === Password field ========================================================
  // Two entries that must match, plus an eyeball to reveal what you typed.
  // Shared so every password prompt behaves identically — a typo in a masked
  // field that only surfaces at the NEXT login is a genuinely bad failure, and
  // it is worse here than usual because a mistyped password locks an account
  // whose recovery path is a shell on the server.
  //
  // Returns { el, value(), valid(), focus() }. `value()` is '' unless both
  // entries match and clear the minimum length.
  function passwordField(opts) {
    opts = opts || {};
    const min = opts.min || 8;
    const idA = opts.id || ('pw_' + Math.floor(performance.now() * 1000));
    const idB = idA + '_confirm';
    // 'new-password' is right when SETTING one (the default), but a login must
    // say 'current-password' or password managers offer to generate instead of
    // to fill.
    const ac0 = opts.autocomplete || 'new-password';
    // The minimum is the placeholder when there IS one to state; a login has no
    // minimum to state, so it says what the box is for instead.
    const hint = opts.placeholder || ('At least ' + min + ' characters');
    const wrap = document.createElement('div');
    const row = (id, label, ac) =>
      '<div class="ed-field" style="margin-bottom:8px"><label for="' + id + '">' + escapeHtml(label) + '</label>' +
      '<div style="display:flex;gap:6px;align-items:stretch">' +
      '<input id="' + id + '" type="password" autocomplete="' + ac + '" ' +
      'placeholder="' + escapeHtml(hint) + '" style="flex:1 1 auto;min-width:0">' +
      '<button type="button" class="ls-pw-eye" data-for="' + id + '" title="Show or hide" ' +
      'aria-label="Show or hide password" style="flex:0 0 auto;padding:0 10px;cursor:pointer">👁</button>' +
      '</div></div>';
    wrap.innerHTML =
      row(idA, opts.label || 'Password', ac0) +
      row(idB, opts.confirmLabel || 'Confirm password', 'new-password') +
      '<div class="ls-pw-msg" style="font-size:.8em;min-height:1.1em;color:var(--muted,#8a7f72)"></div>';

    const a = wrap.querySelector('#' + CSS.escape(idA));
    const b = wrap.querySelector('#' + CSS.escape(idB));
    const msg = wrap.querySelector('.ls-pw-msg');

    // One eye per field rather than a global toggle — you usually want to check
    // just the one you suspect you fumbled.
    wrap.querySelectorAll('.ls-pw-eye').forEach(btn => {
      btn.addEventListener('click', () => {
        const inp = wrap.querySelector('#' + CSS.escape(btn.dataset.for));
        const showing = inp.type === 'text';
        inp.type = showing ? 'password' : 'text';
        btn.textContent = showing ? '👁' : '🙈';
        inp.focus();
      });
    });

    function state() {
      const va = a.value, vb = b.value;
      if (!va && !vb) return { ok: false, msg: '' };
      if (va.length < min) return { ok: false, msg: `At least ${min} characters.` };
      if (!vb) return { ok: false, msg: 'Confirm it.' };
      if (va !== vb) return { ok: false, msg: 'The two entries do not match.' };
      return { ok: true, msg: 'Match.' };
    }
    function refresh() {
      const s = state();
      msg.textContent = s.msg;
      msg.style.color = s.ok ? 'var(--ed-ok,#2f7a3a)' : (s.msg ? 'var(--ed-warn,#a3382b)' : 'var(--muted,#8a7f72)');
      if (typeof opts.onChange === 'function') opts.onChange(s.ok);
    }
    [a, b].forEach(i => i.addEventListener('input', refresh));

    return {
      el: wrap,
      value: () => (state().ok ? a.value : ''),
      valid: () => state().ok,
      message: () => state().msg,
      focus: () => a.focus(),
      clear: () => { a.value = ''; b.value = ''; refresh(); },
    };
  }

  // === Sign-in dialog ========================================================
  // A modal that authenticates in place and hands the caller the result. Built
  // for the staged-grab flow: the bookmarklet identified one user, the browser
  // is someone else (or nobody), and refusing outright would throw away a grab
  // the person can simply fix by signing in.
  //
  // `verify` lets the caller re-check after a successful login and reject it —
  // the dialog then stays open and says so, which is what makes "sign in as the
  // right account" a loop rather than a one-shot. It must not say WHICH account
  // is wanted: whoever holds a stray bookmarklet should not learn whose it is.
  // `mismatchMessage` overrides that wording for callers outside the grab flow.
  //
  // `userId` switches to KNOWN-ACCOUNT mode: the email field disappears and the
  // login posts {user_id, password}. Callers that already know who is signing in
  // — the user switcher clicked a specific row — must not be able to type a
  // different address and land somewhere else. `/auth/login` accepts either key.
  //
  // Resolves {ok:true, user} once verify passes, or {ok:false, cancelled:true}.
  // Cancel is always present — a modal with no way out is a trap.
  function signInDialog(opts) {
    opts = opts || {};
    const byId = opts.userId !== undefined && opts.userId !== null && opts.userId !== '';
    return new Promise(function (resolve) {
      const ov = document.createElement('div');
      ov.style.cssText =
        'position:fixed;inset:0;z-index:1300;background:rgba(42,33,27,.45);' +
        'display:flex;align-items:center;justify-content:center;padding:24px';
      ov.innerHTML =
        '<div style="background:#fff;border-radius:14px;padding:26px 28px;max-width:26rem;' +
        'width:100%;box-shadow:0 12px 36px rgba(60,40,20,.3)">' +
        '<h2 style="margin:0 0 6px;font-size:1.2rem">' + escapeHtml(opts.title || 'Sign in') + '</h2>' +
        '<p style="margin:0 0 16px;color:var(--muted,#6b5b4f);font-size:.9rem;line-height:1.45">' +
        escapeHtml(opts.message || 'Sign in to continue.') + '</p>' +
        (byId ? '' :
          '<div style="margin-bottom:8px"><input class="sd-email" type="email" autocomplete="email" ' +
          'placeholder="you@example.com" style="width:100%;padding:9px 11px;font:inherit;' +
          'border:1px solid var(--border,#e6dccf);border-radius:8px;box-sizing:border-box"></div>') +
        '<div class="sd-pw"></div>' +
        '<div class="sd-msg" style="font-size:.85rem;min-height:1.2em;margin:4px 0 12px;color:#a3382b"></div>' +
        '<div style="display:flex;gap:8px;justify-content:flex-end">' +
        '<button type="button" class="sd-cancel" style="padding:9px 16px;font:inherit;background:none;' +
        'border:1px solid var(--border,#e6dccf);border-radius:8px;cursor:pointer">Cancel</button>' +
        '<button type="button" class="sd-go" style="padding:9px 18px;font:inherit;font-weight:600;' +
        'background:var(--accent,#b8602a);color:#fff;border:none;border-radius:8px;cursor:pointer">Sign in</button>' +
        '</div></div>';
      document.body.appendChild(ov);

      // Reuse the shared password control for the reveal; a login needs no
      // confirm entry, so hide the second field.
      const pwf = passwordField({
        id: 'sd_pw', label: 'Password', min: 1,
        autocomplete: 'current-password', placeholder: 'Your password',
      });
      pwf.el.querySelectorAll('.ed-field')[1].hidden = true;
      ov.querySelector('.sd-pw').appendChild(pwf.el);

      const email = ov.querySelector('.sd-email');   // absent in known-account mode
      const msg = ov.querySelector('.sd-msg');
      const go = ov.querySelector('.sd-go');
      const pwInput = pwf.el.querySelector('input');
      (email || pwInput).focus();

      function close(result) {
        document.removeEventListener('keydown', onKey);
        ov.remove();
        resolve(result);
      }
      function onKey(e) { if (e.key === 'Escape') close({ ok: false, cancelled: true }); }
      document.addEventListener('keydown', onKey);
      ov.querySelector('.sd-cancel').addEventListener('click', () => close({ ok: false, cancelled: true }));

      async function attempt() {
        const em = email ? (email.value || '').trim() : '', pw = pwInput.value || '';
        if (byId ? !pw : (!em || !pw)) {
          msg.style.color = '#a3382b';
          msg.textContent = byId ? 'Enter your password.' : 'Email and password are both required.';
          return;
        }
        go.disabled = true; msg.style.color = 'var(--muted,#6b5b4f)'; msg.textContent = 'Signing in…';
        try {
          const r = await window.fetch('/auth/login', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(byId ? { user_id: Number(opts.userId), password: pw }
                                      : { email: em, password: pw })
          });
          const d = await r.json().catch(() => ({}));
          msg.style.color = '#a3382b';
          if (!r.ok) {
            msg.textContent = d.detail ||
              (byId ? 'Incorrect password.' : 'Incorrect email or password.');
            if (byId) { pwf.clear(); pwInput.focus(); }
            return;
          }
          storeSession(d);
          if (typeof opts.verify === 'function') {
            const okNow = await opts.verify(d);
            if (!okNow) {
              // Signed in successfully, still not the account this grab belongs
              // to. Stay open and let them try another — without naming it.
              msg.textContent = opts.mismatchMessage ||
                'That account doesn’t match this bookmarklet. Try another.';
              pwf.clear();
              if (email) email.select(); else pwInput.focus();
              return;
            }
          }
          // The screen must agree with the new identity immediately.
          try { refreshIdentity(); } catch (e) { /* non-fatal */ }
          close({ ok: true, user: d });
        } catch (e) {
          msg.style.color = '#a3382b';
          msg.textContent = 'Could not reach the server: ' + e.message;
        } finally { go.disabled = false; }
      }
      go.addEventListener('click', attempt);
      [email, pwInput].filter(Boolean).forEach(el =>
        el.addEventListener('keydown', e => { if (e.key === 'Enter') attempt(); }));
    });
  }

  // The CURATOR UNLOCK prompt — the sibling of signInDialog, and not the same
  // credential. signInDialog posts /auth/login, which proves WHO you are; it
  // never mints a master token. A staff account without one resolves as
  // 'member' (staff_locked), so every edit_master endpoint answers 403 and
  // signing in again cannot fix it — the caller has to unlock.
  //
  // This existed only as the header's inline unlock link, so any FLOW that hit a
  // 403 mid-task had no way to ask for the credential it actually needed. The
  // master token lasts MASTER_TOKEN_TTL (12h), so a curator who unlocked
  // yesterday morning is silently demoted today with no prompt anywhere.
  //
  // Returns {ok:true} once /auth/master accepts the password and the token is
  // stored, or {ok:false} if cancelled — same contract as signInDialog so a
  // caller can branch on either without caring which credential was missing.
  function unlockDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      const ov = document.createElement('div');
      ov.style.cssText =
        'position:fixed;inset:0;z-index:1300;background:rgba(42,33,27,.45);' +
        'display:flex;align-items:center;justify-content:center;padding:24px';
      ov.innerHTML =
        '<div style="background:#fff;border-radius:14px;padding:26px 28px;max-width:26rem;' +
        'width:100%;box-shadow:0 12px 36px rgba(60,40,20,.3)">' +
        '<h2 style="margin:0 0 6px;font-size:1.2rem">' + escapeHtml(opts.title || 'Unlock curator tools') + '</h2>' +
        '<p style="margin:0 0 16px;color:var(--muted,#6b5b4f);font-size:.9rem;line-height:1.45">' +
        escapeHtml(opts.message || 'Your curator session has expired. Enter the master password to continue.') + '</p>' +
        '<div class="ud-pw"></div>' +
        '<div class="ud-msg" style="font-size:.85rem;min-height:1.2em;margin:4px 0 12px;color:#a3382b"></div>' +
        '<div style="display:flex;gap:8px;justify-content:flex-end">' +
        '<button type="button" class="ud-cancel" style="padding:9px 16px;font:inherit;background:none;' +
        'border:1px solid var(--border,#e6dccf);border-radius:8px;cursor:pointer">Cancel</button>' +
        '<button type="button" class="ud-go" style="padding:9px 18px;font:inherit;font-weight:600;' +
        'background:var(--accent,#b8602a);color:#fff;border:none;border-radius:8px;cursor:pointer">Unlock</button>' +
        '</div></div>';
      document.body.appendChild(ov);

      const pwf = passwordField({
        id: 'ud_pw', label: 'Master password', min: 1,
        autocomplete: 'current-password', placeholder: 'Master password',
      });
      pwf.el.querySelectorAll('.ed-field')[1].hidden = true;   // no confirm on an unlock
      ov.querySelector('.ud-pw').appendChild(pwf.el);

      const msg = ov.querySelector('.ud-msg');
      const go = ov.querySelector('.ud-go');
      const pwInput = pwf.el.querySelector('input');
      pwInput.focus();

      function close(result) {
        document.removeEventListener('keydown', onKey);
        ov.remove();
        resolve(result);
      }
      function onKey(e) { if (e.key === 'Escape') close({ ok: false }); }
      document.addEventListener('keydown', onKey);
      ov.querySelector('.ud-cancel').addEventListener('click', () => close({ ok: false }));
      ov.addEventListener('click', e => { if (e.target === ov) close({ ok: false }); });

      async function attempt() {
        const pw = pwInput.value || '';
        if (!pw) { msg.textContent = 'Enter the master password.'; return; }
        go.disabled = true; msg.style.color = 'var(--muted,#6b5b4f)'; msg.textContent = 'Unlocking…';
        try {
          // reload:false — the caller has work in flight (a staged capture) that
          // a reload would restart or lose.
          const ok = await unlockAdmin(pw, function (err) {
            msg.style.color = '#a3382b';
            msg.textContent = err || 'Incorrect password.';
          }, { reload: false });
          if (!ok) { pwf.clear(); pwInput.focus(); return; }
          try { refreshIdentity(); } catch (e) { /* non-fatal */ }
          close({ ok: true });
        } catch (e) {
          msg.style.color = '#a3382b';
          msg.textContent = 'Could not reach the server: ' + e.message;
        } finally { go.disabled = false; }
      }
      go.addEventListener('click', attempt);
      pwInput.addEventListener('keydown', e => { if (e.key === 'Enter') attempt(); });
    });
  }

  // What "signed in" MEANS in the browser: the four localStorage keys the
  // patched fetch reads back on every request. Any page that authenticates has
  // to write exactly this set — self_user_id and the legacy sidebar:user_id in
  // step, the session token, and the master token cleared so a previous curator
  // unlock cannot outlive a normal login.
  //
  // One writer because it was three: signInDialog here, plus a hand-copied block
  // in each home page. A key added or renamed in one copy and not the others is
  // a session that half-exists, which surfaces as a permissions error rather
  // than as a login problem.
  function storeSession(d) {
    try {
      localStorage.setItem('app:self_user_id', String(d.user_id));
      localStorage.setItem('sidebar:user_id', String(d.user_id));
      localStorage.setItem('app:session_token', d.token);
      localStorage.removeItem('app:master_token');
    } catch (e) { /* private mode / no storage */ }
  }

  // Re-read identity and rebuild everything that depends on it.
  //
  // The badge hydrates from /auth/me once at page load and fetchAuth() caches
  // the result, so signing in through the dialog changed localStorage and left
  // the screen saying the opposite — you stayed "not signed in" until you
  // navigated. The nav has the same dependency: signing in as staff should
  // reveal the admin burger without a page load.
  //
  // Clearing the cache is the essential part; re-rendering without it would just
  // redisplay the stale answer.
  function refreshIdentity() {
    _authPromise = null;
    document.querySelectorAll('.identity-badge, .nav-toggle, .nav-menu')
            .forEach(el => el.remove());
    if (_lastNavOpts) initNav(_lastNavOpts);
    else initIdentityBadge();
  }
  function initIdentityBadge() {
    const headerInner = document.querySelector('.app-header .header-inner');
    if (!headerInner) return;
    if (headerInner.querySelector('.identity-badge')) return;  // idempotent

    const badge = document.createElement('div');
    badge.className = 'identity-badge';
    badge.innerHTML = '<span class="identity-name muted">…</span>';

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
          // Distinguish "never signed in" from "was signed in, session lapsed".
          // localStorage still naming a user while the server says anonymous is
          // the second case, and it is the one that produced a confusing 403.
          let stale = null;
          try { stale = localStorage.getItem('app:self_user_id'); } catch (e) { /* private mode */ }
          badge.innerHTML = (stale !== null && stale !== '')
            ? '<a class="identity-name" href="/forms/users.html" style="color:#a3382b" ' +
              'title="Your token expired or was cleared — sign in again">session expired</a>'
            : '<a class="identity-name muted" href="/forms/users.html" title="Pick a user">not signed in</a>';
          return;
        }
        const nm = (u.name || u.email || '').trim();
        const uid = u.user_id;
        const role = (data.role || 'member');
        const display = escapeHtml(nm || ('user ' + uid));
        // ONE quiet line (curator, 2026-08-25: the stacked pill was "visually
        // a disaster", especially on mobile). Just the name, ellipsized,
        // linking to the switcher. Email, unlock/lock and sign out live in
        // the ⋮ menu's identity section — the right side IS the area.
        // Elevated staff is the ONE state that gets colour (a mis-click is
        // expensive there) — the signal the recipe form's late persona chip
        // carried before it was removed (2026-08-25).
        const staffCls = data.is_staff ? ' identity-name--staff' : '';
        badge.innerHTML =
          '<a class="identity-name' + staffCls + '" href="/forms/users.html" ' +
          'title="user_id ' + uid + ' · role ' + escapeHtml(role) +
          (data.is_staff ? ' · STAFF ELEVATED' : (data.staff_locked ? ' · staff locked' : '')) +
          ' · click to switch">' + display + '</a>';
      })
      .catch(() => {
        badge.innerHTML = '<span class="identity-name muted">unknown</span>';
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
  // Each item carries a `group`: 'user' or 'admin'. The nav renders TWO
  // hamburgers — a user burger (always shown) and an admin burger (shown
  // only when /auth/me reports an admin role). This separation is deliberate
  // groundwork for the TBOTB (corpus/back-office) vs BCC (personal tool)
  // split: the admin group is the TBOTB-side management surface, the user
  // group is the BCC-side personal cook tool. Reallocate by editing `group`.
  const NAV_ITEMS = [
    // --- user group (the personal cook tool — BCC side) ---
    { page: 'recipes',   label: 'Recipes',   href: '/forms/recipe_form_styled.html', group: 'user' },
    { page: 'cookbooks', label: 'Cookbooks', comingSoon: true, group: 'user' },
    { page: 'equipment', label: 'Equipment', comingSoon: true, group: 'user' },
    { page: 'gourmet',   label: 'Gourmet',   comingSoon: true, group: 'user' },
    { page: 'install',   label: 'Bookmarklet', href: '/forms/install.html', group: 'user' },
    // --- admin group (corpus + system back-office — TBOTB side) ---
    // Kept in alphabetical order by label. `action` items run JS instead of
    // navigating (see initNav wiring).
    { page: 'chapters',  label: 'Chapters',  href: '/forms/chapters.html', perm: 'manage_dishes', group: 'admin' },
    // Two selection techniques, both live. Slash-grouped like the Jobs trio: /Search starts
    // from a saved Amazon search URL and screens the cohort on owner ratings; /Curated starts
    // from a class name and the expert reviews. Both end in product records.
    { page: 'collections', label: 'Collections/Search', href: '/forms/product_collections.html', perm: 'edit_master', group: 'admin' },
    { page: 'curated-collections', label: 'Collections/Curated', href: '/forms/curated_collections.html', perm: 'edit_master', group: 'admin' },
    { page: 'dishes',    label: 'Dishes',    href: '/forms/dishes_v2.html', perm: 'manage_dishes', group: 'admin' },
    // What the corpus holds that the catalog has no record of. Slash-grouped under
    // Dishes because it is the same subject read from the other side: Dishes lists
    // what exists, Dishes/Coverage lists what does not yet.
    { page: 'dish-coverage', label: 'Dishes/Coverage', href: '/forms/dish_coverage.html', perm: 'admin_ui', group: 'admin' },
    { page: 'domains',   label: 'Domains',   href: '/forms/domains.html', perm: 'edit_master', group: 'admin' },
    { page: 'jobs-monitor', label: 'Jobs/Monitor', href: '/forms/jobs_monitor.html', perm: 'admin_ui', group: 'admin' },
    { page: 'run-jobs',  label: 'Jobs/Queued', action: 'runQueuedJobs', perm: 'admin_ui', group: 'admin' },
    { page: 'jobs',      label: 'Jobs/Scheduled', href: '/forms/jobs_admin.html', perm: 'admin_ui', group: 'admin' },
    { page: 'training',  label: 'Labeling', href: '/forms/training.html', perm: 'admin_ui', group: 'admin' },
    { page: 'messages',  label: 'Messages', href: '/forms/admin.html?model=status_messages', perm: 'admin_ui', group: 'admin' },
    { page: 'ingredient-synonyms', label: 'Names', href: '/forms/ingredients.html', perm: 'admin_ui', group: 'admin' },
    { page: 'product-install', label: 'Product Grabber', href: '/forms/product_install.html', perm: 'edit_master', group: 'admin' },
    { page: 'products',  label: 'Products', href: '/forms/products.html', perm: 'edit_master', group: 'admin' },
    { page: 'affiliates', label: 'Affiliates', href: '/forms/affiliates.html', perm: 'edit_master', group: 'admin' },
    { page: 'review-install', label: 'Review Grabber', href: '/forms/review_install.html', perm: 'edit_master', group: 'admin' },
    { page: 'reviews',   label: 'Reviews', href: '/forms/reviews.html', perm: 'edit_master', group: 'admin' },
    { page: 'system',    label: 'System', href: '/forms/system.html', perm: 'configure_system', group: 'admin' },
    { page: 'ws-taxonomy', label: 'Taxonomy', href: '/forms/ws_taxonomy.html', perm: 'admin_ui', group: 'admin' },
    { page: 'cook-kb',   label: 'Tips/Checks', href: '/forms/cook_kb.html', perm: 'edit_master', group: 'admin' },
    { page: 'users',     label: 'Users',     href: '/forms/users.html', perm: 'manage_users', group: 'admin' },
  ];

  // Cached one-shot role probe. Resolves to the role string ('admin',
  // 'member', …) or '' when unknown / not signed in. Shared by the admin
  // burger gate (and anything else that needs to know).
  let _authPromise = null;
  // Cache the WHOLE /auth/me payload, not just the role string. The menu now
  // filters per item on the permission list the server already returns, so
  // throwing everything but `role` away meant re-deriving server truth from a
  // hardcoded role table on the client — which is exactly how isAdminRole drifted
  // (see below).
  function fetchAuth() {
    if (_authPromise) return _authPromise;
    _authPromise = window.fetch('/auth/me')
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => d || {})
      .catch(() => ({}));
    return _authPromise;
  }
  function fetchRole() { return fetchAuth().then(d => d.role || ''); }

  // Kept for callers outside this file. NOTE it is wrong as a staff test —
  // 'editor' and 'author' are staff and hold admin_ui, but this returns false for
  // them, so they never saw the admin burger at all. The menu no longer uses it;
  // it asks for the PERMISSION instead.
  function isAdminRole(role) { return role === 'admin' || role === 'owner'; }

  // Does the caller hold every permission a nav item declares? An item with no
  // `perm` is unrestricted. This is an AFFORDANCE, not a control: hiding a menu
  // entry protects nothing by itself — _require_perm on the endpoint is the
  // control, and the public-host gate is the perimeter. The value here is that
  // the menu stops offering actions that will 403.
  function _itemPermitted(item, perms) {
    const need = item.perm ? [item.perm] : (item.perms || []);
    if (!need.length) return true;
    const have = perms || [];
    return need.every(p => have.indexOf(p) !== -1);
  }

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

  // Draggable-window helper: grab `handle`, move `win` via pointer events
  // (works for mouse + touch). On first grab it pins `win` to fixed
  // positioning at its current spot, so it leaves the overlay's flex
  // centering and stays where you drop it. Stays on-screen.
  function _makeDraggable(win, handle) {
    if (!win || !handle) return;
    handle.style.cursor = 'move';
    handle.style.touchAction = 'none';
    handle.title = 'Drag to move';
    let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
    handle.addEventListener('pointerdown', (e) => {
      const r = win.getBoundingClientRect();
      win.style.position = 'fixed';
      win.style.margin = '0';
      win.style.left = r.left + 'px';
      win.style.top = r.top + 'px';
      ox = r.left; oy = r.top; sx = e.clientX; sy = e.clientY; dragging = true;
      try { handle.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
    });
    handle.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      let nx = ox + (e.clientX - sx), ny = oy + (e.clientY - sy);
      nx = Math.max(0, Math.min(nx, window.innerWidth - 80));   // keep a grab-edge on-screen
      ny = Math.max(0, Math.min(ny, window.innerHeight - 40));
      win.style.left = nx + 'px';
      win.style.top = ny + 'px';
    });
    const end = (e) => { dragging = false; try { handle.releasePointerCapture(e.pointerId); } catch (_) {} };
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
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
    _makeDraggable(overlay.querySelector('.coming-soon-card'),
                   overlay.querySelector('.coming-soon-card h2'));
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

  // Stream ONE already-spawned job into the jobs overlay (used by the dishes
  // Refresh button, which now spawns the job out-of-process and just watches it).
  function streamJob(jobId) {
    _ensureJobsOverlay();
    _setJobsStatus('Running job #' + jobId + '…');
    return _watchJob(jobId, 1, 1).then((status) => {
      _setJobsStatus('Job #' + jobId + ' ' + status + '.');
      _refreshJobBadges();
      return status;
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

  // Build one hamburger (toggle button + dropdown menu) for a set of items.
  // Returns { toggle, menu }. Both are appended by the caller. The menu is
  // mounted on body and positioned under its own toggle on open (so two
  // burgers don't collide at the fixed right edge the CSS would give them).
  // Append the sign-out row to a burger menu.
  //
  // Styles are INLINE rather than in a stylesheet on purpose: the nav CSS is
  // fragmented across nav.css, library-shell.css, editor-shell.css and three
  // pages' own inline blocks (they define their own palettes), so a new class
  // would have to be added in six places to appear everywhere. The var()
  // fallbacks make these two elements render correctly under any of those
  // palettes without touching one of them.
  function _appendSignOut(burger, auth) {
    if (!burger || !burger.menu) return;
    if (burger.menu.querySelector('.nav-signout')) return;      // idempotent
    const sep = document.createElement('div');
    sep.style.cssText = 'border-top:1px solid var(--border,#e6dccf);margin:6px 4px';
    burger.menu.appendChild(sep);
    const u = auth.user || {};
    const uid = u.user_id;
    // WHO row: name + email in one place (was the header pill's job — the
    // pill is now a single quiet name and everything else lives here).
    const whoRow = document.createElement('a');
    whoRow.className = 'nav-item';
    whoRow.href = '/forms/users.html';
    whoRow.title = 'user_id ' + uid + ' · role ' + escapeHtml(auth.role || 'member') + ' · switch user';
    whoRow.innerHTML = escapeHtml((u.name || ('user ' + uid)))
      + (u.email ? '<span style="font-size:.72em;color:var(--muted,#6b5b4f);' +
         'margin-left:8px">' + escapeHtml(u.email) + '</span>' : '');
    burger.menu.appendChild(whoRow);
    // Unlock / lock admin — moved from the header pill. Master (uid 0) IS the
    // token: no lock offered, same rule as before.
    if (uid !== 0 && auth.staff_locked) {
      const un = document.createElement('button');
      un.type = 'button';
      un.className = 'nav-item nav-unlock';
      un.textContent = 'Unlock admin';
      un.title = 'Enter the curator password to enable ' + escapeHtml(auth.actual_role || 'staff') + ' permissions';
      un.addEventListener('click', function (e) {
        e.stopPropagation();
        if (burger.menu.querySelector('.identity-pw')) return;
        const row = document.createElement('div');
        row.className = 'identity-pw';
        row.style.cssText = 'padding:4px 12px 8px';
        row.innerHTML =
          '<input type="password" placeholder="curator password" autocomplete="current-password" ' +
          'style="font-size:12px;padding:3px 6px;width:100%;box-sizing:border-box">' +
          '<div class="identity-pw-msg" style="font-size:11px;margin-top:2px;color:var(--muted,#6b5b4f)"></div>';
        un.after(row);
        const input = row.querySelector('input');
        const msg = row.querySelector('.identity-pw-msg');
        input.addEventListener('click', function (ev) { ev.stopPropagation(); });
        input.focus();
        input.addEventListener('keydown', function (ev) {
          if (ev.key === 'Escape') { row.remove(); return; }
          if (ev.key !== 'Enter' || !input.value) return;
          msg.textContent = 'checking…';
          unlockAdmin(input.value, function (err) { msg.textContent = err; input.value = ''; input.focus(); });
        });
      });
      burger.menu.appendChild(un);
    } else if (uid !== 0 && auth.is_staff) {
      const lk = document.createElement('button');
      lk.type = 'button';
      lk.className = 'nav-item nav-lock';
      lk.textContent = 'Lock admin';
      lk.title = 'Drop staff permissions, stay signed in';
      lk.addEventListener('click', function () { clearMasterToken(); window.location.reload(); });
      burger.menu.appendChild(lk);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'nav-item nav-signout';
    btn.textContent = 'Sign out';
    btn.title = uid === 0 ? 'End the curator session' : "Clear this browser's identity";
    btn.addEventListener('click', signOut);
    burger.menu.appendChild(btn);
  }

  function _buildBurger(items, currentPage, toggleOpts) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'nav-toggle' + (toggleOpts.cls ? ' ' + toggleOpts.cls : '');
    toggle.setAttribute('aria-label', toggleOpts.ariaLabel || 'Open navigation');
    toggle.title = toggleOpts.title || '';
    toggle.innerHTML = '☰';
    if (toggleOpts.bg) toggle.style.background = toggleOpts.bg;

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

    function closeMenu() { menu.classList.remove('open'); }
    function openMenu() {
      // Anchor the menu under THIS toggle. position:FIXED (not the CSS
      // default absolute) so getBoundingClientRect's viewport coords land it
      // correctly — an absolute menu on a scrolled page opens near the
      // document top, off-screen ("have to scroll up to see it"). Fixed makes
      // it overlay the current viewport under the (fixed/sticky) header toggle.
      const r = toggle.getBoundingClientRect();
      menu.style.position = 'fixed';
      menu.style.top = (r.bottom + 6) + 'px';
      menu.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
      menu.classList.add('open');
    }

    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      // Close any other open burger first.
      document.querySelectorAll('.nav-menu.open').forEach(m => { if (m !== menu) m.classList.remove('open'); });
      if (menu.classList.contains('open')) { closeMenu(); return; }
      _refreshJobBadges();
      openMenu();
    });
    document.addEventListener('click', (e) => {
      if (!menu.classList.contains('open')) return;
      if (menu.contains(e.target)) return;
      if (toggle.contains(e.target)) return;
      closeMenu();
    });

    menu.querySelectorAll('.nav-item').forEach(el => {
      const page = el.getAttribute('data-page');
      const cfg = items.find(it => it.page === page);
      if (cfg && cfg.action) {
        el.addEventListener('click', (e) => { e.preventDefault(); closeMenu();
          const fn = NAV_ACTIONS[cfg.action]; if (fn) fn(); });
      } else if (cfg && cfg.comingSoon) {
        el.addEventListener('click', (e) => { e.preventDefault(); closeMenu(); showComingSoon(cfg.label); });
      } else if (page === currentPage) {
        el.addEventListener('click', (e) => { e.preventDefault(); closeMenu(); });
      }
    });

    return { toggle, menu };
  }

  let _lastNavOpts = null;
  function initNav(opts) {
    opts = opts || {};
    _lastNavOpts = opts;
    const currentPage = opts.currentPage || '';
    const allItems = opts.items || NAV_ITEMS;
    const userItems = allItems.filter(it => (it.group || 'admin') === 'user');
    const adminItems = allItems.filter(it => it.group === 'admin');

    const headerInner = document.querySelector('.app-header .header-inner');

    // User burger — always present (accent color, the default look).
    const userBurger = _buildBurger(userItems, currentPage, {
      ariaLabel: 'Open menu', title: 'Menu', cls: 'nav-toggle--user',
    });
    // Admin burger — hidden until the role probe confirms admin. Styled as a
    // distinct SOFT-CLAY chip (.nav-toggle--admin in each shell's CSS) so the two
    // burgers read as different surfaces while staying in the warm palette — no
    // more hardcoded dark "system" fill (which read as garish on the light pages).
    const adminBurger = _buildBurger(adminItems, currentPage, {
      ariaLabel: 'Open admin menu', title: 'Admin', cls: 'nav-toggle--admin',
    });
    adminBurger.toggle.style.display = 'none';

    // If the CURRENT page is an admin page, reveal the admin burger
    // immediately (you're already on it) so it never flickers/vanishes.
    const onAdminPage = adminItems.some(it => it.page === currentPage);

    // Per-item permission filtering. Menus are built synchronously so first
    // paint isn't blocked on /auth/me; when it resolves we remove the entries
    // this caller cannot use. Previously the whole admin burger was shown or
    // hidden on a hardcoded role check, so an 'editor' saw Users and System and
    // got a 403 on click, while an 'author' saw no admin burger at all despite
    // holding edit_master.
    fetchAuth().then(auth => {
      const perms = auth.permissions || [];
      let adminVisible = 0;
      [[userBurger, userItems], [adminBurger, adminItems]].forEach(pair => {
        const burger = pair[0], items = pair[1];
        items.forEach(it => {
          if (_itemPermitted(it, perms)) {
            if (burger === adminBurger) adminVisible++;
            return;
          }
          const node = burger.menu && burger.menu.querySelector('[data-page="' + it.page + '"]');
          if (node) node.remove();
        });
      });
      // Show the admin burger when something is actually in it — not on a role
      // name. An empty menu is worse than no menu.
      if (adminVisible > 0 || onAdminPage) adminBurger.toggle.style.display = '';

      // Sign out goes in BOTH burgers, not only the header identity badge.
      // initIdentityBadge() is called only on pages that have a library-shell
      // header, and ten pages do not — including both home pages — so those
      // offered no way out at all. The account you need to leave is precisely the
      // one whose menu you are stuck inside, so it has to be reachable from
      // whichever burger that account can open.
      //
      // It names WHO you are signing out of. Two accounts here are both called
      // "John Landry", and a page that shows the display name alone cannot tell
      // them apart — which is how a wrong-account problem reads as a broken page.
      if (auth && auth.user) {
        [userBurger, adminBurger].forEach(b => _appendSignOut(b, auth));
      }
    });

    if (headerInner) {
      if (!headerInner.querySelector('.nav-spacer')) {
        const spacer = document.createElement('div');
        spacer.className = 'nav-spacer';
        headerInner.appendChild(spacer);
      }
      // Brand the header here, not only in the sidebar init(). That call sits
      // inside the sidebar setup, so it ran only on pages that HAVE a sidebar —
      // every page adopting the shared header without one (both homes, the three
      // install pages, cook_kb, ingredients, jobs_*, system) got an empty brand
      // bar with a burger floating in it. initNav owns the header chrome, so the
      // brand belongs here. applyBranding is idempotent, so sidebar pages that
      // reach both calls are unaffected.
      applyBranding(headerInner.querySelector('h1'), opts);
      initIdentityBadge();
      // Admin burger sits left of the user burger (back-office tucked inside,
      // personal tool outermost/rightmost).
      headerInner.appendChild(adminBurger.toggle);
      headerInner.appendChild(userBurger.toggle);
    } else {
      // No library-shell header — mount both as fixed top-right buttons.
      //
      // Align to the CONTENT COLUMN, not the viewport edge. Pinning to
      // right:16px stranded the burger far outside the page on a wide screen,
      // on every page using this fallback — and their columns run from 560px
      // (install) to 1200px (jobs monitor), so no single constant works.
      // Measuring .wrap adapts to each without per-page configuration, and
      // falls back to the viewport edge where there is no such column.
      userBurger.toggle.style.cssText += ';position:fixed;top:14px;z-index:101;';
      adminBurger.toggle.style.cssText += ';position:fixed;top:14px;z-index:101;';
      document.body.appendChild(userBurger.toggle);
      document.body.appendChild(adminBurger.toggle);

      const placeBurgers = () => {
        const col = document.querySelector('.wrap, .container, main');
        let right = 16;
        if (col) {
          const r = col.getBoundingClientRect();
          // Sit just inside the column's right edge, but never off-screen on a
          // narrow viewport where the column already spans the full width.
          right = Math.min(Math.max(16, window.innerWidth - r.right + 10),
                           window.innerWidth - 52);
        }
        userBurger.toggle.style.right = right + 'px';
        adminBurger.toggle.style.right = (right + 44) + 'px';
      };
      placeBurgers();
      window.addEventListener('resize', placeBurgers);
      // The column can be laid out after this runs (late CSS, web fonts), so
      // re-measure once the frame settles rather than trusting first paint.
      requestAnimationFrame(placeBurgers);
      window.addEventListener('load', placeBurgers);
    }

    _refreshJobBadges();  // initial queued count on page load
  }

  // ============================================================
  //  Master/detail editor nav controller (a/c/d/v editors)
  // ============================================================
  //
  // The MECHANICAL nav for editor-shell.css pages: dock/overlay list,
  // back-convention control, scrim, mobile drawer, listMode + shellWidth.
  // It is deliberately NOT a data-driven renderer — each editor page is a
  // cloned, hand-written template that supplies its own list/detail render
  // (see memory/project_admin_editor_nav.md + feedback_editor_template_not_runtime).
  //
  // Drives body classes consumed by editor-shell.css:
  //   .ed-mode-overlay  .ed-list-collapsed  .ed-drawer-open
  //
  // Usage:
  //   const nav = LibraryShell.initEditorNav({
  //     backButton: '#edBack', scrim: '#edScrim',
  //     listLabel: 'Chapters', listMode: 'docked', shellWidth: 1200,
  //   });
  //   // after the page selects a list row:  nav.afterSelect();
  // Returns { toggle, afterSelect, setMode, open, close, isOverlay }.
  const _EDNAV_ICONS = {
    back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>',
    collapse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="11 17 6 12 11 7"></polyline><polyline points="18 17 13 12 18 7"></polyline></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>',
  };
  function initEditorNav(opts) {
    opts = opts || {};
    const backBtn = document.querySelector(opts.backButton || '#edBack');
    const scrim = document.querySelector(opts.scrim || '#edScrim');
    const label = opts.listLabel || 'List';
    if (opts.shellWidth != null) {
      const w = (typeof opts.shellWidth === 'number') ? opts.shellWidth + 'px' : opts.shellWidth;
      document.documentElement.style.setProperty('--ed-shell-w', w);
    }
    const mq = window.matchMedia('(max-width: 1024px)');   // match editor-shell.css breakpoint
    const ctl = {
      mode: opts.listMode === 'overlay' ? 'overlay' : 'docked',
      open: true,
      isMobile() { return mq.matches; },
      isOverlay() { return this.mode === 'overlay' || this.isMobile(); },
      render() {
        if (!backBtn) return;
        if (this.isOverlay() && this.open) { backBtn.innerHTML = _EDNAV_ICONS.close; backBtn.title = 'Close ' + label; }
        else if (!this.open) { backBtn.innerHTML = _EDNAV_ICONS.back + '<span>' + escapeHtml(label) + '</span>'; backBtn.title = 'Show ' + label; }
        else { backBtn.innerHTML = _EDNAV_ICONS.collapse; backBtn.title = 'Hide ' + label; }
      },
      apply() {
        const b = document.body;
        b.classList.toggle('ed-mode-overlay', this.mode === 'overlay' && !this.isMobile());
        if (this.isOverlay()) { b.classList.toggle('ed-drawer-open', this.open); b.classList.remove('ed-list-collapsed'); }
        else { b.classList.toggle('ed-list-collapsed', !this.open); b.classList.remove('ed-drawer-open'); }
        this.render();
      },
      toggle() { this.open = !this.open; this.apply(); },
      open_() { this.open = true; this.apply(); },
      close() { this.open = false; this.apply(); },
      afterSelect() { if (this.isOverlay()) { this.open = false; this.apply(); } },
      setMode(m) { this.mode = (m === 'overlay') ? 'overlay' : 'docked'; this.open = !this.isOverlay(); this.apply(); },
    };
    if (backBtn) backBtn.addEventListener('click', () => ctl.toggle());
    if (scrim) scrim.addEventListener('click', () => ctl.close());
    // Reset the default open state when crossing the mobile breakpoint.
    (mq.addEventListener ? mq.addEventListener.bind(mq, 'change') : mq.addListener.bind(mq))(
      () => { ctl.open = !ctl.isOverlay(); ctl.apply(); }
    );
    // Open the list on load: two-pane on desktop, drawer OPEN on mobile/overlay
    // (land on the list, drill into a detail) rather than dropping into a
    // detail with the list hidden.
    ctl.open = true;
    ctl.apply();
    return ctl;
  }

  // ---- Shared URL-field control (memory/feedback_url_field_control) ------------------
  // Every URL field renders with two icon affordances: click-to-open (↗) + copy (⧉).
  // Two modes: display (read-only text + open link + copy) and input (buttons that read a
  // linked <input>'s LIVE value). Self-wires one delegated click handler + minimal CSS.
  let _urlWired = false;
  function _ensureUrlCtl() {
    if (_urlWired) return;
    _urlWired = true;
    const st = document.createElement('style');
    st.textContent =
      // The icons sit INSIDE the input, overlaid on its right edge, rather than
      // beside it. Beside-the-input was two 24px buttons in a flex row that
      // carried `flex-wrap:wrap` — so on a narrow screen they wrapped onto
      // separate lines and stacked vertically instead of staying horizontal.
      // Absolutely positioning them inside removes the wrap failure entirely
      // (there is no row left to wrap) and stops the icons stealing width from
      // the field on a phone.
      '.ls-url-wrap{position:relative;display:block;width:100%}' +
      '.ls-url-wrap>input{width:100%;padding-right:104px}' +
      // nowrap is the fix for the stacking. flex:0 0 auto keeps the group from
      // being squeezed when it is used inline (the read-only display variant).
      '.ls-url-ctl{display:inline-flex;align-items:center;gap:3px;flex-wrap:nowrap;flex:0 0 auto}' +
      '.ls-url-ctl.inside{position:absolute;right:5px;top:50%;transform:translateY(-50%)}' +
      // The read-only variant still wraps: it renders the URL as text and a long
      // one has to break somewhere.
      '.ls-url-ctl.display{flex-wrap:wrap}' +
      '.ls-url-text{font-family:ui-monospace,monospace;font-size:.82rem;word-break:break-all}' +
      '.ls-url-btn{display:inline-flex;align-items:center;justify-content:center;min-width:30px;' +
      'height:30px;padding:0 6px;border:1px solid var(--line,#ccc);border-radius:6px;' +
      'background:var(--card,#fff);cursor:pointer;font-size:.95rem;line-height:1;' +
      'text-decoration:none;color:inherit}' +
      '.ls-url-btn:hover{background:var(--accent-soft,#f0e8e0)}' +
      '.ls-url-btn.disabled{opacity:.4;cursor:default;pointer-events:none}' +
      '.ls-url-clear{color:var(--danger,#c0392b)}' +
      // Full-URL reveal. The native `title` tooltip is kept (free, and it is
      // what a power user reaches for) but it cannot be the answer on its own:
      // it needs about a second of stationary hover, and it does not exist on
      // touch — which is where this started. So an overflowing URL also gets a
      // real popover on hover OR focus, which a tap satisfies.
      // Absolutely positioned so revealing it never reflows the form.
      '.ls-url-full{display:none;position:absolute;z-index:5;left:0;right:0;top:calc(100% + 4px);' +
      'padding:7px 9px;border:1px solid var(--line,#ccc);border-radius:7px;background:var(--card,#fff);' +
      'font-family:ui-monospace,monospace;font-size:.76rem;line-height:1.45;word-break:break-all;' +
      'color:var(--ink,#2a211b);box-shadow:0 8px 22px rgba(60,40,20,.14)}' +
      // Flip ABOVE the field when there is no room below. On a phone, tapping
      // the input opens the keyboard, which covers the bottom of the viewport —
      // so a popover anchored below is exactly where it cannot be read, on the
      // device this was reported from.
      '.ls-url-full.above{top:auto;bottom:calc(100% + 4px)}' +
      '.ls-url-wrap.is-overflow:hover .ls-url-full,' +
      '.ls-url-wrap.is-overflow:focus-within .ls-url-full{display:block}' +
      // Phone: bigger hit areas, and the input grows to hold them.
      '@media(max-width:640px){' +
      '.ls-url-btn{min-width:36px;height:36px;font-size:1.05rem}' +
      '.ls-url-wrap>input{padding-right:122px;min-height:44px}}';
    document.head.appendChild(st);
    document.addEventListener('click', function (e) {
      const c = e.target.closest && e.target.closest('.ls-url-copy');
      if (c) {
        e.preventDefault();
        const v = c.dataset.inputId ? ((document.getElementById(c.dataset.inputId) || {}).value || '') : (c.dataset.url || '');
        if (!v) { flash('No URL to copy', true); return; }
        (navigator.clipboard ? navigator.clipboard.writeText(v) : Promise.reject())
          .then(() => flash('URL copied')).catch(() => flash('Copy failed', true));
        return;
      }
      const o = e.target.closest && e.target.closest('.ls-url-open');
      if (o) {
        e.preventDefault();
        const v = (document.getElementById(o.dataset.inputId) || {}).value || '';
        if (/^https?:\/\//i.test(v)) window.open(v, '_blank', 'noopener');
        else flash('Not a valid link', true);
        return;
      }
      // Clear the field. Dispatches `input` so the page's dirty-tracking sees
      // it — every editor here enables Save off an input event, so clearing
      // without one would leave a field that looks empty and saves as-is.
      const x = e.target.closest && e.target.closest('.ls-url-clear');
      if (x) {
        e.preventDefault();
        const el = document.getElementById(x.dataset.inputId);
        if (!el) return;
        if (!el.value) { flash('Already empty'); return; }
        el.value = '';
        el.removeAttribute('title');
        const w = el.parentElement && el.parentElement.querySelector('.ls-url-full');
        if (w) w.remove();
        if (el.parentElement) el.parentElement.classList.remove('is-overflow');
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.focus();
      }
    });
    // Keep the tooltip + reveal in step with what is actually in the field —
    // otherwise a pasted or edited URL keeps showing the value it replaced,
    // which is worse than showing nothing.
    function _syncUrlReveal(el) {
      const wrap = el && el.parentElement;
      if (!wrap || !wrap.classList || !wrap.classList.contains('ls-url-wrap')) return;
      const v = el.value || '';
      if (v) el.title = v; else el.removeAttribute('title');
      let full = wrap.querySelector('.ls-url-full');
      // Overflow is measured, not guessed: scrollWidth exceeds the visible box
      // only when the value really is clipped, so a short URL never sprouts a
      // popover it does not need.
      const clipped = !!v && el.scrollWidth > el.clientWidth + 2;
      if (clipped) {
        if (!full) {
          full = document.createElement('div');
          full.className = 'ls-url-full';
          wrap.appendChild(full);
        }
        full.textContent = v;
        // Decide the side from the space actually available. Measuring the
        // popover needs it laid out, so make it briefly measurable rather than
        // guessing a height — a long URL wraps to several lines and a fixed
        // guess would flip the wrong way on exactly the URLs that need it.
        full.classList.remove('above');
        const prevDisplay = full.style.display;
        full.style.visibility = 'hidden';
        full.style.display = 'block';
        const box = el.getBoundingClientRect();
        const need = full.getBoundingClientRect().height + 8;
        full.style.display = prevDisplay;
        full.style.visibility = '';
        const roomBelow = (window.innerHeight || 0) - box.bottom;
        // Flip only when below genuinely does not fit AND above does — never
        // trade a bad position for a worse one.
        if (roomBelow < need && box.top > need) full.classList.add('above');
      } else if (full) {
        full.remove();
      }
      wrap.classList.toggle('is-overflow', clipped);
    }
    document.addEventListener('input', function (e) { _syncUrlReveal(e.target); }, true);
    // Measured lazily, at the moment of hover/focus: the field's width is only
    // reliable once it is laid out, and it changes with the viewport.
    document.addEventListener('mouseover', function (e) {
      const w = e.target && e.target.closest && e.target.closest('.ls-url-wrap');
      if (w) _syncUrlReveal(w.querySelector('input'));
    });
    document.addEventListener('focusin', function (e) {
      const w = e.target && e.target.closest && e.target.closest('.ls-url-wrap');
      if (w) _syncUrlReveal(w.querySelector('input'));
    });
  }
  function urlControl(url, opts) {
    opts = opts || {};
    _ensureUrlCtl();
    const u = (url || '').trim(), safe = escapeHtml(u);
    if (opts.inputId) {
      const id = escapeHtml(opts.inputId);
      return '<span class="ls-url-ctl inside">' +
        '<button type="button" class="ls-url-btn ls-url-open" data-input-id="' + id + '" title="Open in new tab" aria-label="Open link">↗</button>' +
        '<button type="button" class="ls-url-btn ls-url-copy" data-input-id="' + id + '" title="Copy URL" aria-label="Copy URL">⧉</button>' +
        '<button type="button" class="ls-url-btn ls-url-clear" data-input-id="' + id + '" title="Clear this URL" aria-label="Clear URL">✕</button></span>';
    }
    const openable = /^https?:\/\//i.test(u);
    const open = openable
      ? '<a class="ls-url-btn" href="' + safe + '" target="_blank" rel="noopener noreferrer" title="Open in new tab" aria-label="Open link">↗</a>'
      : '<span class="ls-url-btn disabled" title="Not a link">↗</span>';
    const copy = u ? '<button type="button" class="ls-url-btn ls-url-copy" data-url="' + safe + '" title="Copy URL" aria-label="Copy URL">⧉</button>' : '';
    const text = (opts.display === false) ? '' : '<span class="ls-url-text">' + (safe || '— none —') + '</span>';
    return '<span class="ls-url-ctl display">' + text + (u ? open + copy : '') + '</span>';
  }

  // Editable URL FIELD (memory/feedback_url_field_control): a labeled <input type=url>
  // with the shared open/copy icons. Uses urlControl's inputId mode, so the ↗/⧉ act on the
  // input's LIVE value via the ONE global delegated handler _ensureUrlCtl wires — no
  // per-form click wiring. One call renders the whole `.ed-field`; the input keeps `id`, so
  // existing save reads (`$('#id').value`) are unchanged. opts: {full, ph}. `label` is
  // inserted as-is (callers may pass small inline markup, matching prior behavior).
  function urlField(id, label, value, opts) {
    opts = opts || {};
    const v = value == null ? '' : String(value);
    const full = opts.full ? ' full' : '';
    const ph = opts.ph ? (opts.ph.endsWith('…') ? opts.ph : 'ex: ' + opts.ph) : '';
    // title = the full URL, so hovering reveals what the field is too narrow to
    // show. The icons occupy ~104px of the input, so a long URL is truncated
    // more than it used to be; the tooltip is what makes it readable without
    // clicking into the field and scrolling.
    return '<div class="ed-field' + full + '"><label for="' + id + '">' + (label == null ? '' : label) + '</label>' +
      '<div class="ls-url-wrap">' +
        '<input id="' + id + '" type="url" value="' + escapeHtml(v) + '" placeholder="' + escapeHtml(ph) + '"' +
          (v ? ' title="' + escapeHtml(v) + '"' : '') + '>' +
        urlControl(v, { inputId: id }) +
      '</div></div>';
  }

  // Retrofit the shared ↗/⧉ icons onto URL inputs that ALREADY exist in static
  // markup (memory/feedback_url_field_control). urlField() builds a field from
  // scratch, which suits the ACDV editors because they render their HTML from
  // template strings — but the recipe form is hand-written markup with save reads
  // bound to specific ids, so rewriting it to use urlField would be a large,
  // risky diff for a cosmetic win. This decorates in place instead: the input
  // keeps its id, type, styles and event handlers untouched.
  //
  // Icons act on the input's LIVE value through the one delegated handler that
  // urlControl's inputId mode already wires, so there is no per-field click
  // plumbing and no second copy implementation.
  function attachUrlControls(ids) {
    (Array.isArray(ids) ? ids : [ids]).forEach(function (id) {
      const input = document.getElementById(id);
      if (!input || input.type === 'hidden') return;
      if (input.parentElement && input.parentElement.classList.contains('ls-url-wrap')) return;  // idempotent
      // Always interpose a positioning wrapper — the icons now sit INSIDE the
      // input, so unlike the old beside-the-input row there is nothing to reuse
      // and nothing that can wrap. Inheriting the input's flex sizing keeps a
      // field that already lived in a flex row (bccPermalink) laid out the same.
      const wrap = document.createElement('div');
      wrap.className = 'ls-url-wrap';
      const cs = (function () { try { return window.getComputedStyle(input); } catch (e) { return null; } })();
      if (cs && cs.flex && cs.flex !== '0 1 auto') wrap.style.flex = cs.flex;
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);
      input.style.flex = '';
      input.style.minWidth = '0';
      if (input.value && !input.title) input.title = input.value;   // hover shows the full URL
      wrap.insertAdjacentHTML('beforeend', urlControl(input.value, { inputId: id }));
    });
  }

  window.LibraryShell = {
    init,
    initNav,
    urlControl,
    urlField,
    attachUrlControls,
    passwordField,
    signInDialog,
    unlockDialog,
    storeSession,
    // The identity probe itself, not just the things built on it. Callers that
    // need to know WHO is signed in before spending money (the staged-grab flow
    // checks before it pays for an extraction) have to be able to ask, and
    // `refreshIdentity` — which invalidates this cache — was already exported
    // without the reader it invalidates.
    fetchAuth,
    fetchRole,
    refreshIdentity,
    initEditorNav,
    initIdentityBadge,
    hostOf,
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
    streamJob,
    queuedJobCount,
    NAV_ITEMS,
    BRAND,
  };
})();
