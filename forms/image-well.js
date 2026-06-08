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
.iw-img{ width:100%; height:100%; object-fit:cover; display:none; }
.iw-frame.iw-has-image .iw-img{ display:block; }
.iw-ph{ display:flex; flex-direction:column; align-items:center; gap:6px; color:var(--iw-muted,#6b5b4f);
  padding:18px; text-align:center; pointer-events:none; }
.iw-frame.iw-has-image .iw-ph{ display:none; }
.iw-ph svg{ width:34px; height:34px; opacity:.55; }
.iw-ph b{ font-size:.95rem; font-weight:600; color:var(--iw-ink,#1f1611); }
.iw-ph small{ font-size:.78rem; }
.iw-overlay{ position:absolute; inset:0; display:flex; gap:8px; align-items:flex-end; justify-content:flex-end;
  padding:10px; opacity:0; transition:opacity .15s; background:linear-gradient(transparent 55%, rgba(0,0,0,.35)); }
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

  function mount(root, opts) {
    opts = opts || {};
    injectStyles();
    const api = opts.api || '';
    const feedback = opts.feedback || function () {};
    let url = (opts.initialUrl || '').trim();

    // ---- default backend handlers (overridable) -------------------------
    const uploadBytes = opts.uploadBytes || async function (file) {
      const fd = new FormData();
      fd.append('image', file, file.name || 'upload');
      const res = await fetch(`${api}/images`, { method: 'POST', body: fd });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'upload failed');
      return `${window.location.origin}${j.url}?t=${Date.now()}`;
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
        if (res.ok && j.url) return `${window.location.origin}${j.url}?t=${Date.now()}`;
      } catch (e) { /* fall through to hotlink */ }
      return s;
    };

    // ---- DOM ------------------------------------------------------------
    // The frame is contenteditable — the ONE trick that makes right-click
    // "Paste" deliver image BYTES (a paste event with the image in
    // clipboardData) AND shows the Paste context-menu item; a plain div gets
    // neither. Children are contenteditable=false so they stay interactive,
    // not editable, and the beforeinput/keydown guards below stop any actual
    // text editing or caret mess. (Same pattern as the recipe drop zone.)
    root.innerHTML =
      `<div class="iw-frame" tabindex="0" role="button" contenteditable="true" aria-label="Set image — paste, drop, or click">
         <img class="iw-img" contenteditable="false" alt="">
         <div class="iw-ph" contenteditable="false">${ICON}<b>Add image</b><small>Right-click → Paste, Ctrl+V, drop, or click</small></div>
         <div class="iw-overlay" contenteditable="false">
           <button type="button" class="iw-btn iw-change">Change</button>
         </div>
       </div>`;
    const frame = root.querySelector('.iw-frame');
    const img = root.querySelector('.iw-img');
    const fileInput = document.createElement('input');
    fileInput.type = 'file'; fileInput.accept = 'image/*'; fileInput.style.display = 'none';
    root.appendChild(fileInput);

    function render() {
      if (url) { img.src = url; frame.classList.add('iw-has-image'); }
      else { img.removeAttribute('src'); frame.classList.remove('iw-has-image'); }
    }
    function setUrl(u, o) {
      url = (u || '').trim();
      render();
      if (!(o && o.silent) && opts.onChange) opts.onChange(url);
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
      try { // optimistic preview
        const dataUrl = await new Promise((res, rej) => {
          const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => rej(r.error);
          r.readAsDataURL(file);
        });
        img.src = dataUrl; frame.classList.add('iw-has-image');
      } catch (e) {}
      feedback('Uploading image…', 'info');
      try { setUrl(await uploadBytes(file)); feedback('Image added. Save to persist.', 'success'); }
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
      const resolved = await resolveUrl(s);
      setUrl(resolved);
      feedback(resolved === s ? 'Using the source link (couldn’t localize). Save to persist.'
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
    }

    render();
    return { getUrl: () => url, setUrl, clear: () => setUrl(''), root, frame };
  }

  window.ImageWell = { mount };
})();
