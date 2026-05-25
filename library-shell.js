/* ============================================================
   library-shell.js
   ------------------------------------------------------------
   Sidebar toggle + iOS body lock + common UI helpers shared by
   list-to-detail admin pages (dishes.html, future cookbooks.html,
   etc.). Sits beside library-shell.css.

   Usage in a page:

       <script src="/forms/library-shell.js"></script>
       <script>
         LibraryShell.init({
           sidebarSelector:        '#sidebar',
           sidebarToggleSelector:  '#sidebarToggle',
         });
       </script>

   The init() call wires:
     - Click on toggle      → opens/closes sidebar + body.sidebar-open lock
     - Click outside sidebar → closes if open (mobile UX)
     - Auto-close on item-pick (call LibraryShell.closeOnNarrow() yourself
       inside your list item's click handler — narrow-screen only)

   Helpers exposed:
     LibraryShell.openSidebar()
     LibraryShell.closeSidebar()
     LibraryShell.toggleSidebar()
     LibraryShell.isNarrow()         // window.matchMedia('(max-width:760px)')
     LibraryShell.closeOnNarrow()    // close sidebar only if narrow viewport
     LibraryShell.escapeHtml(s)
     LibraryShell.fmtDate(iso)       // relative ("3 hr ago") fallback to absolute
   ============================================================ */

(function () {
  const state = {
    sidebar: null,
    sidebarToggle: null,
  };

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

  window.LibraryShell = {
    init,
    openSidebar,
    closeSidebar,
    toggleSidebar,
    isNarrow,
    closeOnNarrow,
    escapeHtml,
    fmtDate,
  };
})();
