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
      closeSidebar();
    });
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
    { page: 'users',     label: 'Users',     href: '/forms/users.html' },
    { page: 'cookbooks', label: 'Cookbooks', comingSoon: true },
    { page: 'equipment', label: 'Equipment', comingSoon: true },
    { page: 'gourmet',   label: 'Gourmet',   comingSoon: true },
    // Utility / setup items sit at the bottom, separated from the
    // entity pages above. "Install bookmarklet" is the most-needed
    // utility today; future items (settings, exports, etc.) go here.
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
      const tag = item.comingSoon ? 'button' : 'a';
      const attrs = item.comingSoon
        ? `type="button" data-page="${escapeHtml(item.page)}"`
        : `href="${escapeHtml(item.href)}" data-page="${escapeHtml(item.page)}"`;
      const badge = item.comingSoon ? '<span class="badge-soon">soon</span>' : '';
      return `<${tag} class="${cls}" ${attrs}>${escapeHtml(item.label)}${badge}</${tag}>`;
    }).join('');
    document.body.appendChild(menu);

    function closeMenu() { menu.classList.remove('open'); }
    function openMenu() { menu.classList.add('open'); }

    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      if (menu.classList.contains('open')) closeMenu(); else openMenu();
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
      if (cfg && cfg.comingSoon) {
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
    openSidebar,
    closeSidebar,
    toggleSidebar,
    isNarrow,
    closeOnNarrow,
    escapeHtml,
    fmtDate,
    renderExcBadge,
    gradeToTier,
    NAV_ITEMS,
  };
})();
