// Recipe-to-form bookmarklet — full logic, loaded by the tiny loader
// bookmarklet served from install.html (desktop + iOS use the same
// loader and the same payload).
//
// The loader does the synchronous window.open() inside the user-gesture
// context (iOS Safari requirement; harmless on desktop), stashes the
// popup handle at window.__recipeBookmarkletPopup, then injects this
// script. By the time THIS code runs, the popup is already open — we
// just navigate it after the staging fetch completes.
//
// Edit this file → update is live on the next bookmark click/tap (the
// loader cache-busts with ?<timestamp>). No re-install ever.
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

  // === Hero image capture ===
  // The bookmarklet runs in the source page's authenticated session,
  // so it can read images the server can't (paywalled, signed-CDN,
  // cookied). Strategy:
  //   1. Find the hero image URL from JSON-LD (or a fallback DOM walk)
  //   2. fetch() it with credentials so cookies + auth headers ride along
  //   3. POST the bytes to our /images endpoint
  //   4. Pass the resulting local URL through stage-markdown so the form
  //      uses it as recipe.image[0] instead of the external URL —
  //      coopting the image so the recipe is permanently independent
  //      of the source site.
  function findHeroImageUrl(jsonldArr) {
    function findRecipe(obj) {
      if (!obj || typeof obj !== 'object') return null;
      const t = obj['@type'];
      if (t === 'Recipe' || (Array.isArray(t) && t.indexOf('Recipe') !== -1)) return obj;
      if (Array.isArray(obj['@graph'])) {
        for (const it of obj['@graph']) {
          const f = findRecipe(it);
          if (f) return f;
        }
      }
      return null;
    }
    function pickUrl(image) {
      if (!image) return null;
      if (typeof image === 'string') return image;
      if (Array.isArray(image)) {
        for (const i of image) { const u = pickUrl(i); if (u) return u; }
        return null;
      }
      if (typeof image === 'object') {
        return image.url || image.contentUrl || image['@id'] || null;
      }
      return null;
    }
    for (const ld of jsonldArr) {
      const r = findRecipe(ld);
      if (r) { const u = pickUrl(r.image); if (u) return u; }
    }
    // Fallback: first reasonably-sized img inside the recipe container.
    const candidates = document.querySelectorAll(
      'article img, main img, [role="main"] img, ' +
      '.recipe-card img, .wprm-recipe-container img, .tasty-recipes img'
    );
    for (const img of candidates) {
      if (img.naturalWidth >= 200 && img.naturalHeight >= 200) {
        return img.currentSrc || img.src;
      }
    }
    return null;
  }

  async function captureHeroImageBytes(heroUrl) {
    // Primary: fetch with credentials. Works for same-origin paywalled
    // images (cookies ride along) and CORS-enabled CDNs. Cap latency
    // so the bookmarklet doesn't stall on hanging requests.
    try {
      const res = await Promise.race([
        fetch(heroUrl, { credentials: 'include' }),
        new Promise((_, rej) => setTimeout(() => rej(new Error('fetch timeout')), 6000))
      ]);
      if (res.ok) {
        const blob = await res.blob();
        if (blob && blob.size > 0 && (blob.type || '').startsWith('image/')) return blob;
      } else {
        console.log('[recipe-bookmarklet] hero fetch HTTP', res.status);
      }
    } catch (e) {
      console.log('[recipe-bookmarklet] hero fetch error:', e && e.message);
    }
    // Fallback: the image is already rendered in the DOM, draw it to a
    // canvas. Works when CORS-fetch is blocked but the <img> tag was
    // loaded with crossorigin=anonymous (lots of news sites do this).
    try {
      const imgs = document.querySelectorAll('img');
      let target = null;
      for (const img of imgs) {
        if ((img.currentSrc || img.src) === heroUrl && img.naturalWidth > 0) {
          target = img; break;
        }
      }
      if (!target) return null;
      const c = document.createElement('canvas');
      c.width = target.naturalWidth;
      c.height = target.naturalHeight;
      c.getContext('2d').drawImage(target, 0, 0);
      return await new Promise((res) => c.toBlob(res, 'image/jpeg', 0.9));
    } catch (e) {
      console.log('[recipe-bookmarklet] canvas fallback failed:', e && e.message);
      return null;
    }
  }

  async function captureAndUploadHero(jsonld) {
    const heroUrl = findHeroImageUrl(jsonld);
    if (!heroUrl) {
      console.log('[recipe-bookmarklet] no hero image URL found in JSON-LD or DOM');
      return null;
    }
    console.log('[recipe-bookmarklet] hero image URL:', heroUrl);
    const blob = await captureHeroImageBytes(heroUrl);
    if (!blob) return null;
    try {
      const fd = new FormData();
      // Choose a sensible extension from the blob's content-type so the
      // server saves it with a useful filename.
      const ext = ((blob.type || 'image/jpeg').split('/')[1] || 'jpg')
                    .replace('jpeg', 'jpg').replace('+xml', '');
      fd.append('image', blob, 'hero.' + ext);
      const uploadRes = await fetch(API + '/images', { method: 'POST', body: fd });
      if (!uploadRes.ok) {
        console.log('[recipe-bookmarklet] hero upload HTTP', uploadRes.status);
        return null;
      }
      const { url } = await uploadRes.json();
      console.log('[recipe-bookmarklet] hero image coopted as', url);
      return url;
    } catch (e) {
      console.log('[recipe-bookmarklet] hero upload failed:', e && e.message);
      return null;
    }
  }

  async function uploadScreenshot(token, root) {
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

    // Scope the screenshot to the recipe-content root (same article /
    // main / .recipe-card element the markdown walker uses), NOT
    // document.body. Whole-body screenshots include comments / ads /
    // footer / related-recipe rails, which on long pages produce
    // 10,000+ px tall images that downscale to unreadably-narrow
    // slivers and blow past Anthropic's vision payload cap.
    const target = root || document.body;
    const rect = target.getBoundingClientRect();
    const targetH = Math.max(target.scrollHeight, target.offsetHeight, rect.height);
    const targetW = Math.max(target.scrollWidth, target.offsetWidth, rect.width);

    const shotPromise = html2canvas(target, {
      height: targetH,
      width: targetW,
      windowHeight: targetH,
      windowWidth: Math.max(targetW, document.body.scrollWidth),
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

    // Client-side downscale + JPEG encoding before upload. The raw
    // canvas can easily be 1500x10000 px for a long recipe page,
    // which as PNG balloons past 10MB — too big to ship and too big
    // for the server's vision-payload cap even after re-downscale.
    // Cap the LONG edge at 2000px (matches the server's _MAX_LONG_EDGE)
    // and encode JPEG q=0.85 (matches the server's downscale settings).
    // Aspect ratio preserved. This produces a payload that's typically
    // 200-800 KB instead of multiple MB.
    const MAX_LONG = 2000;
    let outW = canvas.width;
    let outH = canvas.height;
    const longEdge = Math.max(outW, outH);
    let dataUrl;
    if (longEdge > MAX_LONG) {
      const scale = MAX_LONG / longEdge;
      outW = Math.round(canvas.width * scale);
      outH = Math.round(canvas.height * scale);
      const c2 = document.createElement('canvas');
      c2.width = outW;
      c2.height = outH;
      const ctx = c2.getContext('2d');
      // White fill in case the source canvas had transparent regions —
      // JPEG has no alpha and would blacken them otherwise.
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, outW, outH);
      ctx.drawImage(canvas, 0, 0, outW, outH);
      dataUrl = c2.toDataURL('image/jpeg', 0.85);
    } else {
      // Even under the cap, JPEG-encode the canvas — PNG of a screenshot
      // is wastefully large because every pixel is unique color noise.
      dataUrl = canvas.toDataURL('image/jpeg', 0.85);
    }
    const b64 = dataUrl.split(',')[1];
    console.log('[recipe-bookmarklet] screenshot encoded:',
                canvas.width + 'x' + canvas.height + ' -> ' + outW + 'x' + outH,
                '| b64 chars:', b64.length);
    const uploadRes = await fetch(API + '/stage-image/' + encodeURIComponent(token), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_b64: b64 })
    });
    console.log('[recipe-bookmarklet] screenshot uploaded:', uploadRes.status);
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

    // Coopt the hero image from the user's authenticated browser
    // session BEFORE staging markdown — the local URL rides through
    // the stage payload so the form uses it as image[0]. Failure
    // returns null and we fall through with the external URL still
    // in the JSON-LD; recipe still works, just keeps the external
    // dependency. The whole step is capped at ~8s by inner timeouts
    // so a hanging image fetch can't block the popup.
    const localHeroUrl = await captureAndUploadHero(jsonld);

    const payload = {
      markdown: body,
      source_url: location.href,
      title: document.title,
      local_hero_image_url: localHeroUrl || null
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
      // Pass the recipe `root` so the screenshot scopes to just the
      // recipe content, not the entire document body (which includes
      // comments / ads / footer on long pages).
      await uploadScreenshot(token, root);
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
