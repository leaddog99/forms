// Recipe-to-form bookmarklet (iOS) — full logic, loaded by the tiny loader
// bookmarklet at https://recipes.tbotb.com/forms/bookmarklet_ios.js
//
// The loader does the synchronous window.open() inside the user-gesture
// context (iOS Safari requirement), stashes the popup handle at
// window.__recipeBookmarkletPopup, then injects this script. By the time
// THIS code runs, the popup is already open — we just navigate it after
// the staging fetch completes.
//
// Edit this file → update is live on the next bookmark tap (the loader
// cache-busts with ?<timestamp>). No re-install ever.
(async function () {
  const API = 'https://recipes.tbotb.com';
  const FORM = API + '/forms/recipe_form_styled.html';

  // The loader put the synchronously-opened popup here. If for any reason
  // it's missing (e.g. someone called this script directly without the
  // loader), fall back to opening one ourselves — it will probably be
  // blocked on iOS but at least gives a clear failure path on desktop.
  const popup = window.__recipeBookmarkletPopup || window.open('', '_blank');
  if (!popup) {
    alert('Pop-up blocked. In iOS: Settings → Safari → Block Pop-ups → off. Then re-tap the bookmark.');
    return;
  }
  // Clear the handle from the global so subsequent runs don't reuse a
  // stale tab.
  try { delete window.__recipeBookmarkletPopup; } catch (e) { }

  // Update the popup's placeholder once we're in (it was set to "Loading…"
  // by the loader; this is the more informative version).
  try {
    popup.document.open();
    popup.document.write(
      '<html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:28px">' +
      '<h2>Preparing recipe import...</h2>' +
      '<p>Capturing rendered page content.</p>' +
      '</body></html>'
    );
    popup.document.close();
  } catch (e) { /* cross-origin popup write blocked — fine, ignore */ }

  // === JSON-LD harvest (BEFORE cleanNode strips <script>) ===
  function harvestJsonLd() {
    const out = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
      try {
        const parsed = JSON.parse(s.textContent || '');
        out.push(parsed);
      } catch (e) { /* invalid JSON; skip */ }
    });
    return out;
  }

  function cleanNode(node) {
    const clone = node.cloneNode(true);
    clone.querySelectorAll(
      'script,style,nav,header,footer,aside,noscript,' +
      '.share,.social,.comments,.related,.sidebar,.newsletter,.subscribe,' +
      '[class*="advert"],[id*="advert"],[class*="ad-"],[id*="ad-"],' +
      '[class*="pinterest"],[class*="pin-it"],[class*="affiliate"],[data-affiliate]'
    ).forEach(function (e) { e.remove(); });
    clone.querySelectorAll('img[srcset]').forEach(function (img) {
      img.removeAttribute('srcset');
    });
    return clone;
  }

  const TRACKING_PARAM = /^(utm_|fbclid|gclid|mc_eid|mc_cid|aff_id|igshid|_branch|ref_|hsa_|yclid|msclkid)/i;
  const TRACKING_PARAM_EXACT = /^(tag|ref|affid|partner|source)$/i;
  function cleanHref(href) {
    if (!href) return '';
    try {
      const u = new URL(href, location.href);
      const keep = [];
      u.searchParams.forEach(function (v, k) {
        if (TRACKING_PARAM.test(k)) return;
        if (TRACKING_PARAM_EXACT.test(k)) return;
        keep.push([k, v]);
      });
      const sp = new URLSearchParams();
      keep.forEach(function (kv) { sp.append(kv[0], kv[1]); });
      u.search = sp.toString() ? '?' + sp.toString() : '';
      return u.toString();
    } catch (e) {
      return href;
    }
  }

  function md(node) {
    let out = '';
    node.childNodes.forEach(function (n) {
      if (n.nodeType === 3) { out += n.textContent; return; }
      if (n.nodeType !== 1) return;
      const t = n.tagName.toLowerCase();
      const inner = md(n).trim();
      if (!inner && t !== 'img') return;
      if (t === 'h1') out += '\n# ' + inner + '\n\n';
      else if (t === 'h2') out += '\n## ' + inner + '\n\n';
      else if (t === 'h3') out += '\n### ' + inner + '\n\n';
      else if (t === 'h4') out += '\n#### ' + inner + '\n\n';
      else if (t === 'p') out += '\n' + inner + '\n\n';
      else if (t === 'br') out += '\n';
      else if (t === 'strong' || t === 'b') out += '**' + inner + '**';
      else if (t === 'em' || t === 'i') out += '*' + inner + '*';
      else if (t === 'li') out += '- ' + inner + '\n';
      else if (t === 'ul' || t === 'ol') out += '\n' + inner + '\n';
      else if (t === 'blockquote') out += '\n> ' + inner + '\n\n';
      else if (t === 'a') {
        const cleanedHref = cleanHref(n.href || n.getAttribute('href') || '');
        if (cleanedHref) out += '[' + inner + '](' + cleanedHref + ')';
        else out += inner;
      }
      else if (t === 'img') {
        const src = n.currentSrc || n.src || n.getAttribute('src') || '';
        const alt = n.getAttribute('alt') || '';
        if (src) out += '\n![' + alt + '](' + src + ')\n';
      }
      else out += inner;
    });
    return out;
  }

  async function uploadScreenshot(token) {
    if (!window.html2canvas) {
      await new Promise(function (res, rej) {
        const s = document.createElement('script');
        s.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
        s.onload = res;
        s.onerror = function () { rej(new Error('html2canvas failed to load')); };
        document.head.appendChild(s);
      });
    }
    const BAD = /\b(oklch|oklab|lch|lab|hwb|color-mix|color)\s*\(/i;
    const PROPS = ['color', 'backgroundColor', 'backgroundImage',
      'borderTopColor', 'borderRightColor', 'borderBottomColor', 'borderLeftColor',
      'outlineColor', 'textDecorationColor', 'fill', 'stroke',
      'boxShadow', 'textShadow', 'borderImageSource'];
    const FB = { color: '#111', backgroundColor: 'transparent', backgroundImage: 'none',
      borderTopColor: '#ccc', borderRightColor: '#ccc', borderBottomColor: '#ccc',
      borderLeftColor: '#ccc', outlineColor: 'transparent',
      textDecorationColor: 'currentColor', fill: 'currentColor', stroke: 'currentColor',
      boxShadow: 'none', textShadow: 'none', borderImageSource: 'none' };
    const PLACEHOLDER = 'data:image/svg+xml;utf8,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">' +
      '<rect width="100%" height="100%" fill="#eee"/></svg>');

    const shotPromise = html2canvas(document.body, {
      height: document.body.scrollHeight,
      width: document.body.scrollWidth,
      windowHeight: document.body.scrollHeight,
      windowWidth: document.body.scrollWidth,
      useCORS: true, allowTaint: false, logging: false,
      backgroundColor: '#ffffff', imageTimeout: 8000,
      onclone: function (d) {
        d.querySelectorAll('img').forEach(function (img) {
          try {
            const u = new URL(img.src, location.href);
            if (u.origin !== location.origin) {
              img.src = PLACEHOLDER;
              img.removeAttribute('srcset');
            }
          } catch (e) { img.src = PLACEHOLDER; }
        });
        d.querySelectorAll('[style*="background-image"]').forEach(function (el) {
          const s = el.getAttribute('style') || '';
          if (/url\(/.test(s)) el.style.backgroundImage = 'none';
        });
        d.querySelectorAll('*').forEach(function (el) {
          try {
            const cs = getComputedStyle(el);
            PROPS.forEach(function (p) {
              const v = cs[p];
              if (v && BAD.test(v)) el.style[p] = FB[p];
            });
          } catch (e) { }
        });
      }
    });

    const canvas = await Promise.race([
      shotPromise,
      new Promise(function (_, rej) {
        setTimeout(function () { rej(new Error('screenshot timed out')); }, 45000);
      })
    ]);

    const b64 = canvas.toDataURL('image/png').split(',')[1];
    const uploadRes = await fetch(API + '/stage-image/' + encodeURIComponent(token), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_b64: b64 })
    });
    console.log('[recipe-bookmarklet] screenshot uploaded:', uploadRes.status, '| b64 chars:', b64.length);
  }

  try {
    const root =
      document.querySelector('article') ||
      document.querySelector('main') ||
      document.querySelector('[role="main"]') ||
      document.querySelector('.post-content,.entry-content,.article-content,.post-body,.recipe-card,.wprm-recipe-container,.tasty-recipes') ||
      document.body;

    const jsonld = harvestJsonLd();
    const cleaned = cleanNode(root);

    let body = '# ' + document.title + '\n\n' +
               '*Source: ' + location.href + '*  \n' +
               '*Captured: ' + new Date().toISOString() + '*\n\n';
    if (jsonld.length > 0) {
      body += '## STRUCTURED RECIPE DATA (JSON-LD)\n\n```json\n' +
              JSON.stringify(jsonld, null, 2) + '\n```\n\n';
    }
    body += '---\n\n' + md(cleaned).replace(/\n{3,}/g, '\n\n').trim();

    const payload = {
      markdown: body,
      source_url: location.href,
      title: document.title
    };

    const stageRes = await fetch(API + '/stage-markdown', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!stageRes.ok) throw new Error('Stage failed: HTTP ' + stageRes.status);
    const { token } = await stageRes.json();

    popup.location.href =
      FORM +
      '?url=' + encodeURIComponent(location.href) +
      '&staged=' + encodeURIComponent(token);

    try {
      await uploadScreenshot(token);
    } catch (e) {
      console.log('[recipe-bookmarklet] screenshot skipped/failed:', e && e.message ? e.message : e);
    }
  } catch (e) {
    if (popup && popup.document && popup.document.body) {
      try {
        popup.document.body.innerHTML =
          '<h2>Recipe Import Failed</h2>' +
          '<pre style="white-space:pre-wrap">' +
          String(e && e.message ? e.message : e) +
          '</pre>' +
          '<p style="color:#666;font-size:0.9em">API: ' + API + '<br>Page: ' + location.href + '</p>';
      } catch (writeErr) { /* cross-origin write blocked */ }
    }
    alert('Bookmarklet error:\n\n' +
      (e && e.message ? e.message : e) +
      '\nAPI: ' + API +
      '\nPage: ' + location.href);
  }
})();
