/* list_control.js — drop-in SORT + SEARCH for ANY rendered <table>.
 *
 * Usage on any page that renders a table with <thead>/<tbody>:
 *   <script src="list_control.js"></script>
 *   enhanceTable(tableEl, { searchPlaceholder: 'Search dishes…' });
 *
 * What it does, with zero coupling to the page's data model:
 *   - Clickable <th> headers sort the <tbody> rows by that column
 *     (numeric when the column looks numeric, else locale string),
 *     toggling asc/desc, with a ▲/▼ indicator.
 *   - A search box (inserted just above the table) filters rows by
 *     matching text across all cells, live as you type.
 *
 * It reads <input>/<select>/<textarea>/checkbox VALUES inside cells, so
 * it works on editable admin tables too — and it MOVES existing <tr>
 * nodes rather than rebuilding, so unsaved inline edits survive a sort.
 *
 * Columns are auto-skipped from sorting when their header is blank or
 * marked data-sortable="false" (e.g. an actions column). A cell whose
 * only content is button(s) contributes nothing to sort/search.
 */
(function (global) {
  'use strict';

  function injectCssOnce() {
    if (document.getElementById('lc-style')) return;
    const css = `
      .lc-search{ display:block; width:100%; max-width:340px; margin:0 0 12px;
        border:1px solid var(--hairline,#e5e3df); border-radius:6px;
        background:var(--surface,#fff); color:var(--ink,#1d1d1f);
        font:inherit; font-size:.9em; padding:8px 11px; }
      .lc-search:focus{ outline:none; border-color:var(--ink,#1d1d1f); }
      th.lc-sortable{ cursor:pointer; user-select:none; white-space:nowrap; }
      th.lc-sortable:hover{ color:var(--ink,#1d1d1f); }
      th.lc-sorted{ color:var(--ink,#1d1d1f); }
      .lc-arrow{ color:var(--accent,#8a5a3b); font-size:.9em; }
      .lc-empty{ color:var(--muted,#86868b); font-size:.85em; padding:10px 2px; }`;
    const el = document.createElement('style');
    el.id = 'lc-style'; el.textContent = css;
    document.head.appendChild(el);
  }

  // Sort/search value of a cell: input value if present, blank for a
  // pure-button (actions) cell, else trimmed text.
  function cellValue(td) {
    const field = td.querySelector('input, select, textarea');
    if (field) {
      if (field.type === 'checkbox') return field.checked ? '1' : '0';
      return field.value == null ? '' : String(field.value);
    }
    if (td.querySelector('button') && !td.textContent.trim().replace(/save|delete|edit/gi, '').trim()) return '';
    return td.textContent.trim();
  }

  const NUMERIC = /^\s*-?[\d.,]+%?\s*$/;
  function compare(a, b) {
    if (NUMERIC.test(a) && NUMERIC.test(b)) {
      const na = parseFloat(a.replace(/,/g, '')), nb = parseFloat(b.replace(/,/g, ''));
      if (!isNaN(na) && !isNaN(nb)) return na - nb;
    }
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
  }

  function enhanceTable(table, opts) {
    opts = opts || {};
    if (!table || !table.tHead || !table.tBodies[0]) return;
    injectCssOnce();
    const thead = table.tHead, tbody = table.tBodies[0];
    const headers = Array.from(thead.rows[0].cells);
    let sortCol = -1, sortDir = 1;

    headers.forEach((th, i) => {
      if (th.dataset.sortable === 'false') return;
      if (!th.textContent.trim() && !th.dataset.sortable) return; // actions/blank col
      th.classList.add('lc-sortable');
      th.addEventListener('click', function () {
        sortDir = (sortCol === i) ? -sortDir : 1;
        sortCol = i;
        const rows = Array.from(tbody.rows);
        rows.sort((r1, r2) =>
          compare(cellValue(r1.cells[i]), cellValue(r2.cells[i])) * sortDir);
        rows.forEach(r => tbody.appendChild(r)); // reorder, preserving nodes
        headers.forEach(h => {
          h.classList.remove('lc-sorted');
          const old = h.querySelector('.lc-arrow'); if (old) old.remove();
        });
        th.classList.add('lc-sorted');
        const arrow = document.createElement('span');
        arrow.className = 'lc-arrow'; arrow.textContent = sortDir === 1 ? ' ▲' : ' ▼';
        th.appendChild(arrow);
      });
    });

    if (opts.search !== false) {
      const box = document.createElement('input');
      box.type = 'search'; box.className = 'lc-search';
      box.placeholder = opts.searchPlaceholder || 'Search…';
      let note = null;
      box.addEventListener('input', function () {
        const q = box.value.trim().toLowerCase();
        let shown = 0;
        Array.from(tbody.rows).forEach(r => {
          const hay = Array.from(r.cells).map(cellValue).join(' ').toLowerCase();
          const match = !q || hay.includes(q);
          r.style.display = match ? '' : 'none';
          if (match) shown++;
        });
        if (q && shown === 0) {
          if (!note) { note = document.createElement('div'); note.className = 'lc-empty';
            note.textContent = 'No matches.'; tbody.parentNode.insertBefore(note, tbody.nextSibling); }
        } else if (note) { note.remove(); note = null; }
      });
      const anchor = opts.mount || table;
      anchor.parentNode.insertBefore(box, anchor);
    }
  }

  global.enhanceTable = enhanceTable;
})(window);
