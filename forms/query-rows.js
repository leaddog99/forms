/* query-rows.js — the SEARCH-LINE row editor, shared.
 *
 * Lifted verbatim from forms/dishes_v2.html (2026-09-09) so the product-pool
 * editor looks and acts like the dish form instead of drifting into its own
 * widget. A dish's lines are {q, n, gl, hl, keep}; an Amazon pool's are
 * {q, n, keep} — same rows, same add/remove/Enter/dirty behaviour, same
 * Reserve semantics (a floor of winner seats for that line's own
 * candidates, never a cap). Columns are configured per caller:
 *
 *   QueryRows.init(boxId, rows, opts)      render + wire (opts stored on the box)
 *   QueryRows.blockHtml(boxId, opts)       the markup (header + list + add button)
 *   QueryRows.collect(boxId)               -> {rows} | {error}
 *   QueryRows.paintFetchTotal(boxId, dfltInputId, hintId)
 *
 * opts: { langs: [[code,label],…] | null   // null = no Language/Country columns
 *         nLabel, nPlaceholder, nTitle,     // the count column (Results / Pages)
 *         queryPlaceholder, queryInfoTitle, queryInfo, reserveInfo,
 *         nMax }
 * Requires escapeHtml (library-shell.js) and the .info-dot styling the shell provides.
 */
(function (global) {
  const GL_FOR_LANG = {el:'gr', en:'us', ja:'jp', zh:'cn', ko:'kr', da:'dk', sv:'se', cs:'cz', uk:'ua'};
  const glForLang = (hl) => GL_FOR_LANG[hl] || hl;
  const QIN = 'padding:9px 11px;border:1px solid var(--line,#e2d6c3);border-radius:8px;font:inherit;box-sizing:border-box';
  const _opts = {};   // boxId -> opts
  const esc = (s) => (global.escapeHtml ? global.escapeHtml(s) : String(s ?? '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'));

  const DEFAULTS = {
    langs: null,
    nLabel: 'Results', nPlaceholder: 'dflt', nMax: null,
    nTitle: 'Candidates fetched for THIS line (blank = the default)',
    queryPlaceholder: 'verbatim query — "…" exact, -word / -site:… exclude',
    queryInfoTitle: 'Search lines',
    queryInfo: 'Each line is its own search; results merge and dedupe.',
    reserveInfo: 'Reserve holds this many winner seats for THIS line&#39;s own candidates, ranked among themselves. The remaining seats stay open to everyone (a reserved line&#39;s candidates can win those too — the reserve is a floor, not a cap). Unfilled reserves return to the open pool. Blank = no reservation.',
  };

  function rowHtml(r = {}, o = DEFAULTS){
    let locale = '';
    if (o.langs){
      const langOpts = [['', '— dish default —'], ...o.langs.slice(1)]
        .map(([c, l]) => `<option value="${c}" ${(r.hl || '') === c ? 'selected' : ''}>${l}</option>`).join('');
      locale = `
        <select class="qr-hl" title="Search language for this line (dish default = follow 'Sources in')" style="width:150px;${QIN}">${langOpts}</select>
        <input type="text" class="qr-gl" maxlength="2" placeholder="auto" title="Google country index, 2-letter (auto-derived from the language; override for e.g. English-language results ranking in Greece: language English + country gr)" value="${esc(r.gl || '')}" style="width:56px;text-transform:lowercase;${QIN}">`;
    }
    return `<div class="qrow" style="display:flex;gap:8px;margin-bottom:8px;align-items:flex-start">
      <textarea rows="1" class="qr-q" placeholder='${esc(o.queryPlaceholder)}' style="flex:1;min-width:0;${QIN}">${esc(r.q || '')}</textarea>
      <input type="number" class="qr-n" min="1" ${o.nMax ? `max="${o.nMax}"` : ''} placeholder="${esc(o.nPlaceholder)}" title="${esc(o.nTitle)}" value="${r.n ?? ''}" style="width:70px;${QIN}">
      ${locale}
      <input type="number" class="qr-keep" min="1" placeholder="—" title="Reserved winner seats for this line (ⓘ in the header explains)" value="${r.keep ?? ''}" style="width:56px;${QIN}">
      <button type="button" class="qr-del ed-btn" title="Remove line" style="padding:6px 10px">✕</button>
    </div>`;
  }

  function blockHtml(boxId, opts){
    const o = Object.assign({}, DEFAULTS, opts || {});
    _opts[boxId] = o;
    const localeHead = o.langs ? `<span style="width:150px">Language</span><span style="width:56px">Country</span>` : '';
    return `
      <div id="${boxId}">
        <div style="display:flex;gap:8px;margin-bottom:4px;font-size:.72rem;letter-spacing:.04em;text-transform:uppercase;color:var(--muted,#6b5b4f)">
          <span style="flex:1">Query (verbatim)<button type="button" class="info-dot" data-info-title="${esc(o.queryInfoTitle)}" data-info="${o.queryInfo}">i</button></span><span style="width:70px">${esc(o.nLabel)}</span>${localeHead}<span style="width:56px">Reserve<button type="button" class="info-dot" data-info-title="Reserved winner seats" data-info="${o.reserveInfo}">i</button></span><span style="width:38px"></span>
        </div>
        <div class="qrows-list"></div>
        <button type="button" class="ed-btn qrows-add" style="padding:6px 12px">+ Add line</button>
      </div>`;
  }

  function init(boxId, rows, opts){
    const box = document.getElementById(boxId);
    const o = _opts[boxId] = Object.assign({}, DEFAULTS, _opts[boxId] || {}, opts || {});
    const list = box.querySelector('.qrows-list');
    list.innerHTML = (rows && rows.length ? rows : [{}]).map(r => rowHtml(r, o)).join('');
    // Query boxes grow to fit their content — a long multi-operator query
    // wraps into view instead of scrolling out of sight.
    const autosize = (el) => { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; };
    list.querySelectorAll('.qr-q').forEach(autosize);
    const dirty = () => box.dispatchEvent(new Event('input', {bubbles: true}));
    box.querySelector('.qrows-add').addEventListener('click', () => {
      list.insertAdjacentHTML('beforeend', rowHtml({}, o));
      const q = list.lastElementChild.querySelector('.qr-q');
      autosize(q); q.focus();
      dirty();
    });
    box.addEventListener('click', (e) => {
      const del = e.target.closest('.qr-del'); if (!del) return;
      const row = del.closest('.qrow');
      if (list.children.length > 1) row.remove();
      else {
        // Last line: ✕ means "start over" — clear EVERY field, not just the
        // text (leftover n/locale on an empty line would block the save).
        row.querySelector('.qr-q').value = ''; autosize(row.querySelector('.qr-q'));
        row.querySelector('.qr-n').value = '';
        const hl = row.querySelector('.qr-hl'); if (hl) hl.value = '';
        const gl = row.querySelector('.qr-gl'); if (gl) gl.value = '';
        row.querySelector('.qr-keep').value = '';
      }
      dirty();
    });
    box.addEventListener('input', (e) => {
      const q = e.target.closest('.qr-q'); if (q) autosize(q);
    });
    // A query is ONE search — Enter doesn't make newlines, it adds a line row.
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.closest('.qr-q')){
        e.preventDefault();
        box.querySelector('.qrows-add').click();
      }
    });
    // Picking a language auto-fills its country; still editable afterwards.
    box.addEventListener('change', (e) => {
      const sel = e.target.closest('.qr-hl'); if (!sel) return;
      sel.closest('.qrow').querySelector('.qr-gl').value = sel.value ? glForLang(sel.value) : '';
    });
    // Belt-and-braces dirty signal (2026-08-30): the Reserve spinner edit
    // reached the value without tripping the #page input/change tracker for
    // the curator. Any key or mouse release on a line input IS an edit —
    // dispatch the same bubbling 'input' the add/del buttons use, but only
    // when a value actually differs from what the row rendered with, so
    // mere clicking around never lights Save falsely.
    const _vals = () => JSON.stringify([...box.querySelectorAll('input,textarea,select')].map(i => i.value));
    let _base = _vals();
    box.addEventListener('keyup', () => { if (_vals() !== _base) { _base = _vals(); dirty(); } });
    box.addEventListener('mouseup', () => setTimeout(() => {
      if (_vals() !== _base) { _base = _vals(); dirty(); } }, 0));
  }

  // "120 recs" almost bit twice (curator, 2026-08-28): "Default results per
  // line" reads as a RUN total, but three lines at 120 fetch 360. Say the
  // arithmetic where the number is typed. Sums per-line overrides where set.
  function paintFetchTotal(boxId, dfltId, hintId, unit){
    const hint = document.getElementById(hintId);
    const box = document.getElementById(boxId);
    if (!hint || !box) return;
    const dflt = parseInt(document.getElementById(dfltId)?.value, 10) || 0;
    const ns = [...box.querySelectorAll('.qr-n')].map(i => parseInt(i.value, 10) || dflt);
    const total = ns.reduce((a, b) => a + b, 0);
    const hasOverride = [...box.querySelectorAll('.qr-n')].some(i => i.value.trim());
    hint.textContent = ns.length
      ? `${ns.length} line${ns.length === 1 ? '' : 's'}${hasOverride ? ' (with per-line overrides)' : ` × ${dflt}`} ≈ ${total} ${unit || 'candidates fetched'} per run`
      : '';
  }

  function collect(boxId){
    const o = _opts[boxId] || DEFAULTS;
    const out = [];
    for (const el of document.getElementById(boxId).querySelectorAll('.qrow')){
      const q = el.querySelector('.qr-q').value.trim();
      const nRaw = el.querySelector('.qr-n').value.trim();
      const hlEl = el.querySelector('.qr-hl'), glEl = el.querySelector('.qr-gl');
      const hl = hlEl ? (hlEl.value || null) : null;
      const glRaw = glEl ? glEl.value.trim().toLowerCase() : '';
      const keepRaw = (el.querySelector('.qr-keep')?.value ?? '').trim();
      if (!q){
        // A line with settings but no query would silently vanish on save —
        // say so instead of quietly dropping the curator's work.
        if (nRaw !== '' || hl || glRaw || keepRaw !== '')
          return {error: 'A search line has settings but no query text — type the query, or remove the line with ✕'};
        continue;
      }
      const n = nRaw === '' ? null : parseInt(nRaw, 10);
      if (nRaw !== '' && !(n > 0)) return {error: `${o.nLabel} for “${q}” must be a positive number`};
      if (o.nMax && n > o.nMax) return {error: `${o.nLabel} for “${q}” must be at most ${o.nMax}`};
      if (glRaw && !/^[a-z]{2}$/.test(glRaw)) return {error: `Country for “${q}” must be a two-letter code`};
      const keep = keepRaw === '' ? null : parseInt(keepRaw, 10);
      if (keepRaw !== '' && !(keep > 0)) return {error: `Reserve for “${q}” must be a positive number (or blank)`};
      const row = {q, n, keep};
      if (o.langs){ row.gl = glRaw || (hl ? glForLang(hl) : null); row.hl = hl; }
      out.push(row);
    }
    if (!out.length) return {error: 'At least one search query is required'};
    return {rows: out};
  }

  global.QueryRows = { rowHtml, blockHtml, init, collect, paintFetchTotal, glForLang, GL_FOR_LANG };
})(window);
