/* image-well.js — a reusable image-input control.
 *
 * One target, every input mode (the "recipe box" model applied to images):
 *   • click the well        → opens the Set-image dialog
 *   • drop a file            → uploads it
 *   • paste                  → image bytes upload, OR a URL string is resolved
 *   • enter a URL (dialog)   → resolved (localized) then shown
 *   • Generate (optional)    → host-supplied generator, overlays whatever's there
 * The control "figures it out": image bytes → POST /images; a URL string →
 * POST /images/fetch (coopt/localize) with a hotlink fallback.
 *
 * Reusable by design: it owns the UX; the HOST passes the backend handlers +
 * an onChange callback, so the same control drops into the recipe form today
 * and the master/dish editors later. It injects its own CSS (no external
 * stylesheet dependency) so it renders correctly on any page.
 *
 *   const well = ImageWell.mount(rootEl, {
 *     initialUrl, onChange(url), feedback(msg,kind),
 *     generate: async () => url|null,        // optional → shows Generate
 *     uploadBytes: async (file) => url,      // default: POST /images
 *     resolveUrl:  async (urlStr) => url,    // default: POST /images/fetch
 *     api: '',                               // base URL prefix
 *   });
 *   well.getUrl(); well.setUrl(url, {silent}); well.clear();
 */
(function () {
  let _stylesInjected = false;
  function injectStyles() {
    if (_stylesInjected) return;
    _stylesInjected = true;
    const css = `
.iw-frame{ position:relative; width:100%; aspect-ratio:3/2; border:1.5px dashed var(--iw-border,#d8cfc0);
  border-radius:14px; background:var(--iw-bg,#faf6f1); overflow:hidden; cursor:pointer;
  display:flex; align-items:center; justify-content:center; transition:border-color .15s, background .15s; }
.iw-frame:hover{ border-color:var(--iw-accent,#b8602a); }
.iw-frame.iw-drag{ border-color:var(--iw-accent,#b8602a); background:var(--iw-accent-soft,rgba(184,96,42,.08)); }
.iw-frame.iw-has-image{ border-style:solid; }
.iw-frame:focus-visible{ outline:2px solid var(--iw-accent,#b8602a); outline-offset:2px; }
.iw-frame, .iw-dz{ caret-color:transparent; } /* contenteditable for image paste only — no visible caret */
.iw-img{ width:100%; height:100%; object-fit:cover; display:none; pointer-events:none; } /* let right-click reach the editable frame for Paste */
.iw-frame.iw-has-image .iw-img{ display:block; }
.iw-ph{ display:flex; flex-direction:column; align-items:center; gap:6px; color:var(--iw-muted,#6b5b4f);
  padding:18px; text-align:center; }
.iw-frame.iw-has-image .iw-ph{ display:none; }
/* The placeholder TEXT is a real editable caret target (no contenteditable=false,
   not pointer-events:none) so right-clicking the empty well lands the caret in
   text and Chrome ENABLES "Paste" — same as the dialog drop-zone. Only the SVG
   icon ignores pointer events. */
.iw-ph svg{ width:34px; height:34px; opacity:.55; pointer-events:none; }
.iw-ph b{ font-size:.95rem; font-weight:600; color:var(--iw-ink,#1f1611); }
.iw-ph small{ font-size:.78rem; }
.iw-overlay{ position:absolute; inset:0; display:flex; gap:8px; align-items:flex-end; justify-content:flex-end;
  padding:10px; opacity:0; transition:opacity .15s; background:linear-gradient(transparent 55%, rgba(0,0,0,.35));
  pointer-events:none; } /* the overlay covers the frame at inset:0 — must NOT intercept right-click/paste; only the buttons do */
.iw-overlay .iw-btn{ pointer-events:auto; }
.iw-frame:hover .iw-overlay, .iw-frame:focus-within .iw-overlay{ opacity:1; }
.iw-frame:not(.iw-has-image) .iw-overlay{ background:none; }
.iw-btn{ font:inherit; font-size:.82rem; font-weight:600; padding:7px 13px; border-radius:9px; cursor:pointer;
  border:1px solid rgba(255,255,255,.7); background:rgba(255,255,255,.92); color:var(--iw-ink,#1f1611);
  -webkit-backdrop-filter:blur(4px); backdrop-filter:blur(4px); }
.iw-btn:hover{ background:#fff; }
.iw-btn.iw-gen{ background:var(--iw-accent,#b8602a); color:#fff; border-color:var(--iw-accent,#b8602a); }
.iw-btn.iw-gen:hover{ background:var(--iw-accent-dark,#944a1f); }
.iw-btn:disabled{ opacity:.55; cursor:default; }
/* dialog */
.iw-dialog{ border:none; border-radius:16px; padding:0; max-width:440px; width:92vw;
  box-shadow:0 20px 60px rgba(40,25,10,.35); color:var(--iw-ink,#1f1611); }
.iw-dialog::backdrop{ background:rgba(30,20,12,.45); }
.iw-dialog-in{ padding:22px; }
.iw-dialog h3{ margin:0 0 4px; font-size:1.1rem; }
.iw-dialog p.iw-sub{ margin:0 0 16px; color:var(--iw-muted,#6b5b4f); font-size:.86rem; }
.iw-dz{ border:1.5px dashed var(--iw-border,#d8cfc0); border-radius:12px; padding:22px; text-align:center;
  color:var(--iw-muted,#6b5b4f); font-size:.88rem; cursor:pointer; transition:border-color .15s, background .15s; }
.iw-dz:hover, .iw-dz.iw-drag{ border-color:var(--iw-accent,#b8602a); background:var(--iw-accent-soft,rgba(184,96,42,.08)); }
.iw-or{ display:flex; align-items:center; gap:10px; margin:16px 0 12px; color:var(--iw-muted,#6b5b4f); font-size:.76rem; }
.iw-or::before, .iw-or::after{ content:''; flex:1; height:1px; background:var(--iw-border,#e6dccf); }
.iw-urlrow{ display:flex; gap:8px; }
.iw-urlrow input{ flex:1; padding:10px 12px; border:1px solid var(--iw-border,#d8cfc0); border-radius:9px;
  font:inherit; font-size:.9rem; }
.iw-genpanel{ margin-top:14px; padding-top:14px; border-top:1px solid var(--iw-border,#e6dccf); }
.iw-genpanel textarea{ width:100%; font:inherit; font-size:.88rem; padding:9px 11px; border:1px solid var(--iw-border,#d8cfc0); border-radius:9px; }
.iw-genpanel summary{ cursor:pointer; font-size:.82rem; color:var(--iw-muted,#6b5b4f); user-select:none; }
.iw-actions{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin-top:18px; }
.iw-actions .iw-right{ display:flex; gap:8px; }
.iw-dialog .iw-pri{ background:var(--iw-accent,#b8602a); color:#fff; border-color:var(--iw-accent,#b8602a); }
.iw-dialog .iw-pri:hover{ background:var(--iw-accent-dark,#944a1f); }
`;
    const s = document.createElement('style');
    s.setAttribute('data-image-well', '');
    s.textContent = css;
    document.head.appendChild(s);
  }

  const ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' +
    '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>' +
    '<path d="M21 15l-5-5L5 21"/></svg>';

  // Canonical client-side downscale for ANY image before upload (single path —
  // hero/dish photo AND recipe-extract photo go through this). Shrinks a photo
  // so a weak network can actually push it, while keeping enough resolution that
  // recipe text stays OCR-legible: long edge <= 1568px (the vision model's
  // effective ceiling — larger gets resized down server-side anyway) at JPEG
  // ~0.88. A 12MP/2-4MB iPhone photo becomes ~1.5MP / ~250-450KB (~8-10x lighter).
  // Best-effort: returns the ORIGINAL file unchanged on a non-image, a decode
  // failure, an already-small image, or no size win — never blocks an upload.
  const UPLOAD_MAX_EDGE = 1568;
  const UPLOAD_JPEG_QUALITY = 0.88;
  async function downscaleForUpload(file) {
    try {
      if (!file || !file.type || !file.type.startsWith('image/')) return file;
      let bmp;
      try {
        // Apply EXIF orientation so a portrait photo isn't re-baked sideways.
        bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
      } catch (_) {
        bmp = await createImageBitmap(file); // older engines: option unsupported
      }
      const longEdge = Math.max(bmp.width, bmp.height);
      if (longEdge <= UPLOAD_MAX_EDGE) { bmp.close && bmp.close(); return file; }
      const scale = UPLOAD_MAX_EDGE / longEdge;
      const w = Math.round(bmp.width * scale), h = Math.round(bmp.height * scale);
      const canvas = document.createElement('canvas');
      canvas.width = w; canvas.height = h;
      canvas.getContext('2d').drawImage(bmp, 0, 0, w, h);
      bmp.close && bmp.close();
      const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', UPLOAD_JPEG_QUALITY));
      if (!blob || blob.size >= file.size) return file; // no win — keep original
      const name = (file.name || 'photo').replace(/\.[^.]+$/, '') + '.jpg';
      return new File([blob], name, { type: 'image/jpeg', lastModified: file.lastModified || Date.now() });
    } catch (_) {
      return file;
    }
  }

  function mount(root, opts) {
    opts = opts || {};
    injectStyles();
    const api = opts.api || '';
    const feedback = opts.feedback || function () {};
    let url = (opts.initialUrl || '').trim();

    // ---- default backend handlers (overridable) -------------------------
    // Both return { url, meta } — meta is the server's imageMeta block
    // (width/height/format/bytes/orientation/localized…) or null.
    const uploadBytes = opts.uploadBytes || async function (file) {
      const fd = new FormData();
      fd.append('image', file, file.name || 'upload');
      const res = await fetch(`${api}/images`, { method: 'POST', body: fd });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'upload failed');
      return { url: `${window.location.origin}${j.url}?t=${Date.now()}`, meta: j.imageMeta || null };
    };
    // Resolve a URL string to a (preferably localized) image URL. Coopt via
    // /images/fetch; on any failure fall back to the original (hotlink).
    const resolveUrl = opts.resolveUrl || async function (s) {
      try {
        const res = await fetch(`${api}/images/fetch`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: s }),
        });
        const j = await res.json().catch(() => ({}));
        if (res.ok && j.url) return { url: `${window.location.origin}${j.url}?t=${Date.now()}`, meta: j.imageMeta || null };
      } catch (e) { /* fall through to hotlink */ }
      return { url: s, meta: null };  // hotlink fallback, no local meta
    };
    // Normalize a handler result that may be a bare string (custom host
    // handlers) or { url, meta }.
    const _norm = (r) => (typeof r === 'string' ? { url: r, meta: null } : (r || { url: '', meta: null }));

    // ---- DOM ------------------------------------------------------------
    // The frame is contenteditable — the trick that makes right-click "Paste"
    // deliver image BYTES + surface the Paste menu item. CRITICAL: NO child may
    // be contenteditable=false — a non-editable island (especially the overlay,
    // which covers the whole frame at inset:0) makes Chrome/Edge see a
    // non-editable region at the click point and GREYS OUT Paste. The recipe
    // drop-zone works precisely because all its children are plain editable
    // content. The beforeinput/keydown guards below block actual text edits.
    root.innerHTML =
      `<div class="iw-frame" tabindex="0" contenteditable="true" aria-label="Set image — paste, drop, or click">
         <img class="iw-img" alt="">
         <div class="iw-ph">${ICON}<b>Add image</b><small>Right-click → Paste, Ctrl+V, drop, or click</small></div>
         <div class="iw-overlay">
           <button type="button" class="iw-btn iw-change">Change</button>
         </div>
       </div>`;
    const frame = root.querySelector('.iw-frame');
    const img = root.querySelector('.iw-img');
    // A zero-width editable text node gives the contenteditable frame a caret
    // position — without it Chrome won't offer "Paste" in the right-click menu
    // on an otherwise-empty editable div.
    frame.insertBefore(document.createTextNode('​'), frame.firstChild);
    const fileInput = document.createElement('input');
    fileInput.type = 'file'; fileInput.accept = 'image/*'; fileInput.style.display = 'none';
    root.appendChild(fileInput);

    function render() {
      if (url) { img.src = url; frame.classList.add('iw-has-image'); }
      else { img.removeAttribute('src'); frame.classList.remove('iw-has-image'); }
    }
    let meta = opts.initialMeta || null;   // server imageMeta for the current url
    function setUrl(u, o) {
      url = (u || '').trim();
      meta = (o && 'meta' in o) ? o.meta : (url ? meta : null);
      render();
      if (!(o && o.silent) && opts.onChange) opts.onChange(url, meta);
    }
    img.addEventListener('load', () => {
      const w = img.naturalWidth, h = img.naturalHeight;
      if (w && h) frame.style.aspectRatio = `${w} / ${h}`;
    });
    img.addEventListener('error', () => frame.style.removeProperty('aspect-ratio'));

    // ---- input handling -------------------------------------------------
    async function handleFile(file) {
      if (!file) return;
      if (!file.type || !file.type.startsWith('image/')) {
        feedback(`Not an image (${file.type || 'unknown type'})`, 'error'); return;
      }
      file = await downscaleForUpload(file); // shrink before upload (weak-network friendly)
      try { // optimistic preview
        const dataUrl = await new Promise((res, rej) => {
          const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => rej(r.error);
          r.readAsDataURL(file);
        });
        img.src = dataUrl; frame.classList.add('iw-has-image');
      } catch (e) {}
      feedback('Uploading image…', 'info');
      try { const r = _norm(await uploadBytes(file)); setUrl(r.url, { meta: r.meta }); feedback('Image added. Save to persist.', 'success'); }
      catch (e) { feedback(`Upload failed: ${e.message || e}`, 'error'); }
    }
    async function handleUrlString(s) {
      s = (s || '').trim();
      if (!s) return;
      if (s.startsWith('blob:') || s.startsWith('data:')) {
        feedback(`That's a ${s.startsWith('blob:') ? 'browser-internal (blob:)' : 'embedded (data:)'} URL — ` +
                 `it won't work outside the page that made it. Save the image and drop the file instead.`, 'error');
        return;
      }
      if (!/^https?:\/\//i.test(s)) { feedback('Enter an http(s) image URL.', 'error'); return; }
      feedback('Fetching image…', 'info');
      const r = _norm(await resolveUrl(s));
      const hotlinked = (r.url === s);
      setUrl(r.url, { meta: r.meta });
      feedback(hotlinked ? 'Using the source link (couldn’t localize). Save to persist.'
                         : 'Image saved locally. Save to persist.', 'success');
    }
    // bytes-or-url from a clipboard/drag event
    function handleDataTransfer(dt) {
      if (!dt) return false;
      const file = dt.files && dt.files[0];
      if (file) { handleFile(file); return true; }
      const items = dt.items || [];
      for (const it of items) {
        if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
          const f = it.getAsFile(); if (f) { handleFile(f); return true; }
        }
      }
      const text = dt.getData && dt.getData('text');
      if (text && /^https?:\/\//i.test(text.trim())) { handleUrlString(text); return true; }
      return false;
    }

    // Ctrl+V doesn't require focusing the well: a capture-phase document paste
    // handler scoped to "mouse is over the well" pastes the image and stops the
    // event before the form's recipe-extraction paste handler sees it. So the
    // user can copy an image, hover the well, and Ctrl+V — no click, no dialog.
    let _hovering = false;
    frame.addEventListener('mouseenter', () => { _hovering = true; });
    frame.addEventListener('mouseleave', () => { _hovering = false; });
    document.addEventListener('paste', (e) => {
      if (!_hovering) return;
      const cd = e.clipboardData; if (!cd) return;
      for (const it of (cd.items || [])) {
        if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
          const f = it.getAsFile();
          if (f) { e.preventDefault(); e.stopImmediatePropagation(); e._handled = true; handleFile(f); return; }
        }
      }
      const text = cd.getData && cd.getData('text');
      if (text && /^https?:\/\//i.test(text.trim())) {
        e.preventDefault(); e.stopImmediatePropagation(); e._handled = true; handleUrlString(text);
      }
    }, true);  // capture phase = fire before the bubble-phase recipe paste handler

    // Right-click: focus the frame + drop a caret into the editable anchor
    // BEFORE the context menu opens, so Chrome ENABLES the "Paste" item (it
    // greys Paste out when there's no editable caret in the target). mousedown
    // (button 2) fires before contextmenu.
    frame.addEventListener('mousedown', (e) => {
      if (e.button !== 2) return;
      frame.focus();
      try {
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(frame);
        range.collapse(true);          // caret at the start (the ZWSP anchor)
        sel.removeAllRanges(); sel.addRange(range);
      } catch (_) {}
    });

    // frame: drag/drop/paste/click
    frame.addEventListener('dragover', (e) => { e.preventDefault(); frame.classList.add('iw-drag'); });
    frame.addEventListener('dragleave', (e) => {
      if (e.relatedTarget && frame.contains(e.relatedTarget)) return; frame.classList.remove('iw-drag');
    });
    frame.addEventListener('drop', (e) => {
      e.preventDefault(); e.stopPropagation(); frame.classList.remove('iw-drag');
      handleDataTransfer(e.dataTransfer);
    });
    frame.addEventListener('paste', (e) => {
      const cd = e.clipboardData; if (!cd) return;
      // image bytes?
      for (const it of (cd.items || [])) {
        if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
          const f = it.getAsFile(); if (f) { e.preventDefault(); e.stopPropagation(); e._handled = true; handleFile(f); return; }
        }
      }
      const text = cd.getData && cd.getData('text');
      if (text && /^https?:\/\//i.test(text.trim())) { e.preventDefault(); e.stopPropagation(); e._handled = true; handleUrlString(text); }
    });
    frame.addEventListener('click', (e) => {
      if (e.target.closest('.iw-btn')) return; // overlay buttons handle themselves
      openDialog();
    });
    frame.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDialog(); } });
    // The frame is contenteditable only to enable image paste (right-click +
    // Ctrl+V). Block every actual content mutation so nothing can be typed,
    // pasted-as-text, or dropped-as-text into it — our paste/drop handlers above
    // do the real work and preventDefault on the cases they handle.
    frame.addEventListener('beforeinput', (e) => e.preventDefault());
    const changeBtn = root.querySelector('.iw-change');
    if (changeBtn) changeBtn.addEventListener('click', (e) => { e.stopPropagation(); openDialog(); });
    fileInput.addEventListener('change', (e) => { const f = e.target.files[0]; if (f) handleFile(f); fileInput.value = ''; });
    // Generate lives ONLY in the Set-image dialog (one button, no overlay/dialog
    // duplication). The overlay is just "Change" → opens the dialog.

    // ---- Set-image dialog ----------------------------------------------
    let dialog = null;
    function buildDialog() {
      dialog = document.createElement('dialog');
      dialog.className = 'iw-dialog';
      dialog.innerHTML =
        `<form method="dialog" class="iw-dialog-in">
           <h3>Set image</h3>
           <p class="iw-sub">Right-click → Paste, Ctrl+V, drop a file, or click to choose — image or URL. We’ll save a local copy when we can.</p>
           <div class="iw-dz" tabindex="0" contenteditable="true">Right-click → Paste, Ctrl+V, or drop an image here, or <u>choose a file</u></div>
           <div class="iw-or">or paste / type a URL</div>
           <div class="iw-urlrow">
             <input type="url" placeholder="https://…/photo.jpg" class="iw-urlin">
             <button type="button" class="iw-btn iw-pri iw-seturl">Set</button>
           </div>
           ${opts.generate && opts.generatePanel ? `<div class="iw-genpanel">${opts.generatePanel}</div>` : ''}
           <div class="iw-actions">
             <span>${opts.generate ? '<button type="button" class="iw-btn iw-dgen">✨ Generate</button>' : ''}</span>
             <div class="iw-right">
               ${url ? '<button type="button" class="iw-btn iw-remove">Remove</button>' : ''}
               <button type="button" class="iw-btn iw-cancel">Done</button>
             </div>
           </div>
         </form>`;
      document.body.appendChild(dialog);
      const dz = dialog.querySelector('.iw-dz');
      const urlin = dialog.querySelector('.iw-urlin');
      // dz is contenteditable ONLY so right-click "Paste" delivers image bytes;
      // block real editing so it stays a button, not a text box.
      dz.addEventListener('beforeinput', (e) => e.preventDefault());
      dz.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
      dz.addEventListener('click', () => fileInput.click());
      dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('iw-drag'); });
      dz.addEventListener('dragleave', () => dz.classList.remove('iw-drag'));
      dz.addEventListener('drop', (e) => { e.preventDefault(); dz.classList.remove('iw-drag'); if (handleDataTransfer(e.dataTransfer)) dialog.close(); });
      dz.addEventListener('paste', (e) => { if (handleDataTransfer(e.clipboardData)) dialog.close(); });
      // Catch a Ctrl+V image ANYWHERE in the open dialog (focus is usually on
      // the url input). Image bytes → upload + close. stopPropagation keeps the
      // form's document-level paste handler (recipe extraction) from also
      // grabbing it. Non-image (a URL string) falls through to the url input.
      dialog.addEventListener('paste', (e) => {
        for (const it of ((e.clipboardData && e.clipboardData.items) || [])) {
          if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
            const f = it.getAsFile();
            if (f) { e.preventDefault(); e.stopPropagation(); e._handled = true; handleFile(f); dialog.close(); return; }
          }
        }
      });
      const doSet = () => { const v = urlin.value.trim(); if (v) { handleUrlString(v); dialog.close(); } };
      dialog.querySelector('.iw-seturl').addEventListener('click', doSet);
      urlin.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); doSet(); } });
      dialog.querySelector('.iw-cancel').addEventListener('click', () => dialog.close());
      const rm = dialog.querySelector('.iw-remove');
      if (rm) rm.addEventListener('click', () => { setUrl(''); dialog.close(); });
      const dgen = dialog.querySelector('.iw-dgen');
      if (dgen && opts.generate) dgen.addEventListener('click', async () => {
        dgen.disabled = true; const lbl = dgen.textContent; dgen.textContent = 'Generating…';
        try { const u = await opts.generate(); if (u) setUrl(u); dialog.close(); }
        catch (err) { feedback(`Generate failed: ${err.message || err}`, 'error'); }
        finally { dgen.disabled = false; dgen.textContent = lbl; }
      });
    }
    function openDialog() {
      // Rebuild each open so the Remove button + url field reflect current state.
      if (dialog) { dialog.remove(); dialog = null; }
      buildDialog();
      const urlin = dialog.querySelector('.iw-urlin');
      urlin.value = '';
      if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
      // Focus a paste-capable element so Ctrl+V / Shift+Insert work immediately
      // (an image paste fires here and is caught by the dialog paste handler;
      // a URL paste lands in the field).
      try { urlin.focus(); } catch (e) {}
      // Let the host populate dialog fields it owns (e.g. restore the saved
      // generate-tweak into #imageGenExtraPrompt) after the dialog is built.
      if (opts.onDialogOpen) { try { opts.onDialogOpen(dialog); } catch (e) {} }
    }

    render();
    return { getUrl: () => url, getMeta: () => meta, setUrl, clear: () => setUrl(''), root, frame };
  }

  window.ImageWell = { mount, downscaleForUpload };
})();
