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
  // The app host is NOT hardcoded here (portable-package: no data in code). It
  // rides in on the loader, which sets window.__recipeBookmarkletApi and the
  // <script src> both to the configured origin (served by install.html from
  // system_config.public_base_url). We read it back from there.
  const API = (function () {
    try { if (window.__recipeBookmarkletApi) return String(window.__recipeBookmarkletApi); } catch (e) {}
    try { const cs = document.currentScript; if (cs && cs.src) return new URL(cs.src).origin; } catch (e) {}
    try { const sc = document.querySelector('script[src*="bookmarklet.js"]'); if (sc && sc.src) return new URL(sc.src).origin; } catch (e) {}
    return location.origin; // last resort (correct only when run on the app itself)
  })();
  const FORM = API + '/forms/recipe_form_styled.html';

  // NO KEY. This bookmarklet is UNIVERSAL — one install, any user, any number
  // of devices. It stages the page and redirects to the form, and the form saves
  // under whoever is signed in; ownership was always decided there, never here.
  // A key would have been a second identity to keep in sync with that one, which
  // is exactly the mismatch it then had to police.
  //
  // Not signed in? The form raises the sign-in dialog and saves to whoever
  // authenticates. The product and review grabbers DO carry keys, because they
  // post to gated endpoints and act immediately with no form and no session.

  // Loader version. The payload you are reading is cache-busted on every click,
  // so logic changes ship without a re-install — but the one-liner in the
  // bookmarks bar is frozen at install time. Bump BOTH this and the loader when
  // the one-liner's shape changes, and an old install gets told to re-install
  // instead of failing in some confusing way.
  const LOADER_V = 3;
  const loaderV = (function () {
    try { return parseInt(window.__bccLoaderV, 10) || 0; } catch (e) { return 0; }
  })();

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

  // Send the popup to the form NOW, before we read a single node of this page.
  // The form is on our own origin, so it can see the session immediately and
  // resolve identity — or put up the login — WHILE the extraction and staging
  // below are still running. Previously the tab sat on a splash until staging
  // finished, so a not-signed-in user only met the login after all that work.
  //
  // The token is delivered afterwards as a URL FRAGMENT (see below), because a
  // fragment change fires `hashchange` without reloading — navigating properly
  // would destroy a login dialog the user might be in the middle of typing
  // into. Cross-origin we may set `location.href` but not read it, hence
  // rebuilding the exact same URL rather than appending to the current one.
  // EVERY write into the popup happens BEFORE the hand-off, never after.
  // `document.open()` aborts whatever that window is currently loading, so
  // writing a placeholder into a tab we just pointed at the form CANCELS the
  // navigation — which is exactly what it did: the popup sat on about:blank
  // until the final `location.href` at the end of staging, so identity resolved
  // AFTER the grab rather than alongside it and the parallel design above was
  // dead on arrival. Nothing looked wrong, because the second navigation
  // carried the token and read as a normal load.
  //
  // An old one-liner still works for now, but say so once rather than letting it
  // drift silently until something actually breaks.
  if (loaderV < LOADER_V) {
    try {
      popup.document.open();
      popup.document.write(
        '<html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:28px;line-height:1.5">' +
        '<h2>Your bookmarklet is out of date</h2>' +
        '<p>It still works, but a newer version is available. Re-install it from ' +
        '<a href="' + API + '/forms/install.html">the install page</a> when convenient.</p>' +
        '<p style="color:#6b5b4f;font-size:.9em">Continuing in a moment&hellip;</p>' +
        '</body></html>');
      popup.document.close();
    } catch (e) { /* cross-origin write blocked */ }
    await new Promise(r => setTimeout(r, 2500));
  }

  const AWAIT_URL = FORM + '?awaiting=1&url=' + encodeURIComponent(location.href);
  let popupHandedOff = false;
  // Did the popup ACTUALLY take a navigation? Tracked separately from
  // popupHandedOff so that, when every attempt to drive it fails, the capture
  // is not simply lost — the recipe is already staged on the server, so all the
  // user needs is a way to reach it. See the rescue banner after staging.
  let popupNavigated = false;
  let finalUrl = AWAIT_URL;

  // NOTE: there is no postMessage channel to the popup. `window.open('','_blank')`
  // followed by a cross-origin navigation to the form SEVERS `window.opener`
  // (verified 2026-07-31: the popup reports opener === null), so neither side can
  // message the other. The fragment below is best-effort; the form's own
  // /staged-latest poll is what actually completes the hand-off.
  try {
    popup.location.href = AWAIT_URL;
    popupHandedOff = true;
    popupNavigated = true;
  } catch (e) {
    // Navigation refused — fall back to the original behaviour further down,
    // which navigates once with ?staged= after the token exists.
    console.log('[recipe-bookmarklet] early hand-off failed:', e && e.message);
  }

  // Only when the hand-off did NOT happen is there an empty tab left to fill.
  // When it did, the form IS the placeholder — and a better one, since it can
  // take a login while the capture below runs.
  if (!popupHandedOff) {
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
  }

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
    //
    // THEN retry WITHOUT credentials. This is not belt-and-braces, it is the
    // common case for a third-party image CDN: per the Fetch spec, a wildcard
    // `Access-Control-Allow-Origin: *` is REJECTED when the request's
    // credentials mode is 'include' — the wildcard is only honoured for
    // anonymous requests. So `credentials:'include'` POISONS exactly the CDNs
    // that are most willing to serve us.
    //
    // Measured 2026-08-14 on i2.chuimg.com (xiachufang's image CDN): it
    // returns `Access-Control-Allow-Origin: *`, so the credentialed fetch
    // fails CORS and the un-credentialed one succeeds. Before this, both the
    // fetch and the canvas fallback (tainted, no crossorigin attr on the
    // <img>) failed, the coopt returned null, and the recipe kept the
    // publisher's remote URL — which then 403'd in the browser because that
    // CDN is referer-gated. One missing retry cost us the whole hero.
    for (const creds of ['include', 'omit']) {
      try {
        const res = await Promise.race([
          fetch(heroUrl, { credentials: creds }),
          new Promise((_, rej) => setTimeout(() => rej(new Error('fetch timeout')), 6000))
        ]);
        if (res.ok) {
          const blob = await res.blob();
          if (blob && blob.size > 0 && (blob.type || '').startsWith('image/')) {
            console.log('[recipe-bookmarklet] hero fetched credentials=' + creds +
                        ' (' + blob.size + ' bytes)');
            return blob;
          }
        } else {
          console.log('[recipe-bookmarklet] hero fetch HTTP', res.status,
                      'credentials=' + creds);
        }
      } catch (e) {
        console.log('[recipe-bookmarklet] hero fetch error (credentials=' + creds + '):',
                    e && e.message);
      }
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

  // Minimum size we trust as a "real" screenshot. A page screenshot
  // at 2000px long edge / JPEG q=0.85 is typically 100-500 KB; below
  // ~22 KB suggests html2canvas captured a near-empty subtree (e.g.,
  // the wrong root container — see pickBestRoot's regression notes).
  // We retry with document.body in that case so the image-extraction
  // fallback gets actual page content to OCR.
  const SCREENSHOT_MIN_B64_CHARS = 30000;

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

    // Capture+encode helper — extracted so we can call it twice
    // (once with the picked root, fallback with document.body if
    // the first capture comes out suspiciously small). Returns
    // { b64, canvasW, canvasH, outW, outH }.
    async function captureAndEncode(target) {
      const rect = target.getBoundingClientRect();
      const targetH = Math.max(target.scrollHeight, target.offsetHeight, rect.height);
      const targetW = Math.max(target.scrollWidth, target.offsetWidth, rect.width);

      // WINDOW WIDTH DECIDES WHICH MEDIA QUERIES RUN, so it must match the layout
      // the user is actually looking at. This passed
      // max(targetW, document.body.scrollWidth), and scrollWidth is routinely
      // inflated by an overflowing ad or embed — on a phone that laid the clone
      // out at ~1900px, so DESKTOP css applied inside a 390px-wide capture: the
      // article column rendered at a quarter width, the hero image clipped off
      // the right, the text microscopic. It was always this broken; at 65-73px
      // wide the result was simply too small to read as wrong (measured on two
      // captures from 2026-08-13 01:59 and 02:05, before any change).
      //
      // innerWidth is the honest answer — the viewport the page is currently
      // rendered for. Falling back to targetW keeps the element from being laid
      // out narrower than the region we are about to capture.
      const viewW = Math.max(window.innerWidth || 0, targetW);
      const shotPromise = html2canvas(target, {
        height: targetH,
        width: targetW,
        windowHeight: targetH,
        windowWidth: viewW,
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
      // Client-side downscale + JPEG encoding before upload.
      //
      // CAP THE WIDTH, NOT THE LONG EDGE. A recipe capture is a tall ribbon, so
      // the long edge is the HEIGHT — and scaling the height down to 2000 drags
      // the width with it. On a desktop (1200 x 3000) that still left 800px of
      // width and looked fine; on a PHONE (390 CSS px x DPR 3 = 1170 wide, and
      // 18000 tall for a long page) it scaled by 0.11 and produced a 65 x 2000
      // sliver. Measured 2026-08-13: four phone captures stored at 65x35, 81x43,
      // 115x61 and 138x74 — correctly proportioned, unreadably small.
      //
      // Width is the dimension that matters: the server frames the capture to an
      // above-fold window of width x (427/800) and never upscales, so whatever
      // width arrives is the ceiling on quality. A second guard bounds total
      // pixels, which is what actually protects a phone's encoder (and Safari's
      // canvas limit) — the thing the long-edge cap was reaching for.
      const MAX_W = 1600;
      const MAX_PIXELS = 12e6;
      let outW = canvas.width;
      let outH = canvas.height;
      let scale = 1;
      if (outW > MAX_W) scale = MAX_W / outW;
      if (outW * outH * scale * scale > MAX_PIXELS) {
        scale = Math.sqrt(MAX_PIXELS / (outW * outH));
      }
      let dataUrl;
      if (scale < 1) {
        outW = Math.max(1, Math.round(canvas.width * scale));
        outH = Math.max(1, Math.round(canvas.height * scale));
        const c2 = document.createElement('canvas');
        c2.width = outW;
        c2.height = outH;
        const ctx = c2.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, outW, outH);
        ctx.drawImage(canvas, 0, 0, outW, outH);
        dataUrl = c2.toDataURL('image/jpeg', 0.85);
      } else {
        dataUrl = canvas.toDataURL('image/jpeg', 0.85);
      }
      const b64 = dataUrl.split(',')[1];
      return { b64: b64, canvasW: canvas.width, canvasH: canvas.height,
               outW: outW, outH: outH };
    }

    // First attempt — the picked root (typically the recipe-content
    // container). Whole-body screenshots include comments / ads /
    // footer / related-recipe rails, which on long pages produce
    // 10,000+ px tall images.
    const target = root || document.body;
    let captured = await captureAndEncode(target);
    console.log('[recipe-bookmarklet] screenshot[root] encoded:',
                captured.canvasW + 'x' + captured.canvasH + ' -> ' +
                captured.outW + 'x' + captured.outH +
                ' | b64 chars: ' + captured.b64.length);

    // Size sanity — a real recipe screenshot is normally 100-500 KB
    // (133K-666K b64 chars). Under SCREENSHOT_MIN_B64_CHARS suggests
    // we captured a nearly-empty subtree (root picker landed on a
    // wrapper that contains no rendered content). Retry with the
    // full body so the image-extraction LLM fallback gets useful
    // pixels. Skip the retry if we already targeted body.
    if (captured.b64.length < SCREENSHOT_MIN_B64_CHARS && target !== document.body) {
      console.log('[recipe-bookmarklet] screenshot too small (' +
                  Math.round(captured.b64.length * 3 / 4 / 1024) +
                  'KB), retrying with document.body');
      try {
        const retry = await captureAndEncode(document.body);
        console.log('[recipe-bookmarklet] screenshot[body] encoded:',
                    retry.canvasW + 'x' + retry.canvasH + ' -> ' +
                    retry.outW + 'x' + retry.outH +
                    ' | b64 chars: ' + retry.b64.length);
        // Use the retry only if it's bigger. (If body comes out
        // smaller too, the page genuinely has minimal content;
        // keep the root attempt rather than swap to something
        // potentially worse.)
        if (retry.b64.length > captured.b64.length) {
          captured = retry;
        }
      } catch (retryErr) {
        console.log('[recipe-bookmarklet] body-retry failed:',
                    retryErr && retryErr.message);
      }
    }

    const uploadRes = await fetch(API + '/stage-image/' + encodeURIComponent(token), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_b64: captured.b64 })
    });
    console.log('[recipe-bookmarklet] screenshot uploaded:', uploadRes.status);
  }

  // === Recipe-content scoring (for root selection) ===
  // Inline subset of server's RECIPE_PHRASES — just the most reliable
  // signals. Counting hits at the bookmarklet level catches the
  // case where the FIRST <article>/<main> match is a page wrapper
  // (header, breadcrumbs, sidebar nav) rather than the recipe-
  // content container — pre-2026-05-27 the walker committed to that
  // wrong root, captured no recipe text, and the LLM extracted 0
  // ingredients. Now we score every candidate and pick the best.
  const RECIPE_PHRASES = [
    'teaspoon', 'tablespoon', 'tsp', 'tbsp', 'cup', 'cups',
    ' oz', ' lb', ' lbs', ' ounce', ' pound', 'gram', ' ml',
    'ingredients', 'instructions', 'directions', 'method',
    'prep time', 'cook time', 'total time', 'serves',
    'servings', 'yield',
    'preheat', 'bake', 'boil', 'simmer', 'roast', 'fry',
    'minutes', 'whisk'
  ];

  function scoreText(text) {
    const lower = (text || '').toLowerCase();
    let phraseHits = 0;
    for (const p of RECIPE_PHRASES) {
      if (lower.indexOf(p) !== -1) phraseHits++;
    }
    const chars = (text || '').length;
    // Char count is decent but phrases are far more reliable — a
    // sidebar nav can be 5000 chars of menu items with 0 phrases.
    // Weight phrases ~100x so 10 phrases beats 1000 chars of menu.
    return { chars: chars, phraseHits: phraseHits, score: chars + 100 * phraseHits };
  }

  function pickBestRoot() {
    // Build candidate list in priority order; dedupe at the end.
    const candidates = [];
    const addAll = function (sel) {
      try {
        document.querySelectorAll(sel).forEach(function (el) { candidates.push(el); });
      } catch (e) { /* invalid selector, skip */ }
    };
    // Schema.org Recipe wrapper — strongest semantic signal when present
    addAll('[itemtype*="Recipe"]');
    addAll('[typeof*="Recipe"]');
    // Recipe-plugin containers (high signal)
    addAll('.wprm-recipe-container');
    addAll('.tasty-recipes');
    addAll('.mv-recipe-card');
    addAll('.recipe-card');
    addAll('.recipe');
    // Mediavine "create" recipe-card wrappers (cleanfoodiecravings,
    // many other food blogs running Mediavine ads). The recipe lives
    // in `.recipe-details` which contains `.recipe-ingredient` +
    // `.recipe-instruction` siblings — outside the page's <article>.
    addAll('.recipe-details');
    addAll('[data-slot-rendered-recipe]');
    // hRecipe microdata (older standard, still in use)
    addAll('[class*="hrecipe"]');
    // Article / main — the old default
    addAll('article');
    addAll('main');
    addAll('[role="main"]');
    // Common WordPress / blog content wrappers
    addAll('.post-content');
    addAll('.entry-content');
    addAll('.article-content');
    addAll('.post-body');
    // Body always last as a fallback
    candidates.push(document.body);

    const seen = new Set();
    const unique = candidates.filter(function (el) {
      if (!el || seen.has(el)) return false;
      seen.add(el);
      return true;
    });

    // Score each candidate. cleanNode + md is moderately expensive
    // (DOM clone + tree walk), but for a typical page with <10
    // candidates this is <100ms total — well within "click feels
    // instant" budget.
    let best = null;
    const scored = [];
    for (const el of unique) {
      const cleanedEl = cleanNode(el);
      const text = md(cleanedEl).trim();
      const s = scoreText(text);
      scored.push({ el: el, text: text, score: s });
      if (!best || s.score > best.score.score) {
        best = { el: el, text: text, score: s };
      }
    }

    // Diagnostic log so investigating "why did it pick X" doesn't
    // require re-bookmarketting with a debugger attached.
    console.log('[recipe-bookmarklet] root candidates:',
      scored.map(function (x) {
        return { tag: x.el.tagName,
                 cls: (x.el.className || '').toString().slice(0, 40),
                 chars: x.score.chars,
                 phrases: x.score.phraseHits,
                 score: x.score.score };
      })
    );
    console.log('[recipe-bookmarklet] picked root:', best.el.tagName,
      '(' + (best.el.className || '').toString().slice(0, 40) + ')',
      'score=' + best.score.score, 'phrases=' + best.score.phraseHits);

    return { el: best.el, mdText: best.text };
  }

  try {
    // Multi-candidate root pick — see pickBestRoot above. Falls back
    // to document.body if no candidate has any recipe content.
    const picked = pickBestRoot();
    const root = picked.el;
    const mdText = picked.mdText;

    const jsonld = harvestJsonLd();

    let body = '# ' + document.title + '\n\n' +
               '*Source: ' + location.href + '*  \n' +
               '*Captured: ' + new Date().toISOString() + '*\n\n';
    if (jsonld.length > 0) {
      body += '## STRUCTURED RECIPE DATA (JSON-LD)\n\n```json\n' +
              JSON.stringify(jsonld, null, 2) + '\n```\n\n';
    }
    body += '---\n\n' + mdText.replace(/\n{3,}/g, '\n\n').trim();

    // Coopt the hero image from the user's authenticated browser
    // session BEFORE staging markdown — the local URL rides through
    // the stage payload so the form uses it as image[0]. Failure
    // returns null and we fall through with the external URL still
    // in the JSON-LD; recipe still works, just keeps the external
    // dependency. The whole step is capped at ~8s by inner timeouts
    // so a hanging image fetch can't block the popup.
    const localHeroUrl = await captureAndUploadHero(jsonld);

    // Harvest manual-from-reject hints from the URL fragment. The dish
    // form stamps reject links with #_bcc_dish=<name>&_bcc_run=<iso>
    // (forms/dishes.html renderRejects). When present, the resulting
    // save will be force-targeted to master with _master.dish/run stamped
    // so the rescued recipe attributes back to its originating batch.
    // Stripped from source_url so the canonical URL stays clean.
    const bccHints = {};
    let canonicalSourceUrl = location.href;
    try {
      const hash = (location.hash || '').replace(/^#/, '');
      if (hash) {
        const params = new URLSearchParams(hash);
        const dish = params.get('_bcc_dish');
        const run = params.get('_bcc_run');
        // A PUBLISHER capture has no dish to name, but is just as much a corpus
        // grab. Same hint channel, so there is ONE way to say "this belongs to
        // master" — the previous mechanism (the queue writing
        // localStorage['sidebar:user_id']='0') was a global that a bookmarklet
        // press outside the queue never set, and that persisted afterwards to
        // mis-target the next personal grab.
        const master = params.get('_bcc_master');
        if (dish) bccHints.dish = dish;
        if (run) bccHints.run = run;
        if (master) bccHints.master = true;
        if (dish || run || master) {
          // Rebuild a URL without our hints in the fragment so source_url
          // reflects what the source site sees, not our internal plumbing.
          params.delete('_bcc_dish');
          params.delete('_bcc_run');
          params.delete('_bcc_master');
          const remaining = params.toString();
          canonicalSourceUrl = location.origin + location.pathname + location.search
            + (remaining ? '#' + remaining : '');
        }
      }
    } catch (e) { /* malformed hash, fall through with no hints */ }

    const payload = {
      markdown: body,
      source_url: canonicalSourceUrl,
      title: document.title,
      local_hero_image_url: localHeroUrl || null,
      bcc_hints: Object.keys(bccHints).length ? bccHints : null
    };

    const stageRes = await fetch(API + '/stage-markdown', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!stageRes.ok) throw new Error('Stage failed: HTTP ' + stageRes.status);
    const { token } = await stageRes.json();

    if (popupHandedOff) {
      // Hand the token over by postMessage, with the fragment as a fallback.
      //
      // The fragment ALONE is a race, and it loses often enough to matter: if
      // staging finishes before the popup has COMMITTED its navigation to
      // AWAIT_URL, then setting AWAIT_URL + '#staged=…' is a second navigation
      // to the same document that the browser coalesces with the first — it
      // commits the fragment-less one and the token is silently dropped. No
      // hashchange fires and the form waits on a message that will never come.
      // Reproduced on christinascucina.com 2026-07-31 (popup showed hash:''
      // and history.length:1, proving the second navigation never applied),
      // while healthline.com had worked minutes earlier on identical code —
      // which is the tell for a race rather than a broken path.
      //
      // Still sent: when it DOES win the race it is faster than the form's next
      // poll tick, and it keeps older form builds working. When it loses, the
      // form's /staged-latest poll finds the grab by source url instead.
      try { popup.location.href = AWAIT_URL + '#staged=' + encodeURIComponent(token); }
      catch (e) { /* popup gone */ }
    } else {
      // Fallback: the early hand-off didn't happen, so this is the first
      // navigation and the form reads ?staged= from the query as before.
      //
      // This assignment used to be UNPROTECTED, and that turned an unusable
      // popup into a silent dead end: the early hand-off fails (caught, so no
      // form ever opens), staging succeeds, then THIS line throws and jumps to
      // the outer catch — so uploadScreenshot never runs either. Observed
      // 2026-08-11 on an allrecipes egg foo young: the server log shows
      // stage-markdown 200 and then nothing at all, no form request and no
      // stage-image, while the capture itself had completed perfectly.
      finalUrl =
        FORM +
        '?url=' + encodeURIComponent(location.href) +
        '&staged=' + encodeURIComponent(token);
      try { popup.location.href = finalUrl; popupNavigated = true; }
      catch (e) {
        console.log('[recipe-bookmarklet] popup navigation failed:', e && e.message);
      }
    }

    try {
      // Pass the recipe `root` so the screenshot scopes to just the
      // recipe content, not the entire document body (which includes
      // comments / ads / footer on long pages).
      await uploadScreenshot(token, root);
    } catch (e) {
      console.log('[recipe-bookmarklet] screenshot skipped/failed:', e && e.message ? e.message : e);
    }

    // RESCUE. The grab succeeded and is sitting on the server under `token`,
    // but no window was ever driven to it, so from the user's side this looks
    // like a bookmarklet that did nothing — the failure mode that reads as a
    // hang. Put an unmissable, unblockable link on the page they are already
    // looking at: a plain anchor they click, so no popup blocker is involved
    // and the source page is not navigated away from underneath them.
    if (!popupNavigated) {
      try {
        const id = '__bcc_rescue';
        const old = document.getElementById(id);
        if (old) old.remove();
        const bar = document.createElement('div');
        bar.id = id;
        bar.setAttribute('style', [
          'position:fixed', 'z-index:2147483647', 'left:16px', 'right:16px', 'bottom:16px',
          'background:#1c1813', 'color:#f4efe6', 'padding:16px 18px', 'border-radius:12px',
          'font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
          'box-shadow:0 8px 30px rgba(0,0,0,.35)', 'display:flex', 'gap:14px',
          'align-items:center', 'flex-wrap:wrap'
        ].join(';'));
        const msg = document.createElement('span');
        msg.textContent = 'Recipe captured — the pop-up could not be opened.';
        msg.style.flex = '1 1 220px';
        const a = document.createElement('a');
        a.href = finalUrl;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = 'Open your recipe →';
        a.setAttribute('style',
          'background:#e0a75e;color:#1c1813;padding:10px 16px;border-radius:8px;' +
          'text-decoration:none;font-weight:600;white-space:nowrap');
        const x = document.createElement('button');
        x.textContent = '✕';
        x.setAttribute('style',
          'background:transparent;border:0;color:#f4efe6;font-size:18px;cursor:pointer;padding:4px 8px');
        x.onclick = () => bar.remove();
        bar.appendChild(msg); bar.appendChild(a); bar.appendChild(x);
        document.body.appendChild(bar);
      } catch (e) {
        // Even the banner failed (hostile CSP / no body) — say it plainly
        // rather than leaving the user with nothing at all.
        alert('Recipe captured, but the pop-up could not be opened.\n\n'
              + 'Open this to finish:\n' + finalUrl);
      }
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

// ============================================================================
// EXECUTABLE BOOKMARKLET — copy this single line into a bookmark's URL.
// ============================================================================
// This is the LOADER: it opens the popup synchronously (iOS requirement), then
// injects THIS file cache-busted (?Date.now()), so editing bookmarklet.js is
// live on the very next click — no re-install. The loader itself rarely
// changes; only re-copy this if the loader (not the logic above) is edited.
//
// Don't copy this by hand — install.html emits the loader with the configured
// host (system_config.public_base_url) baked in. The host below is only a
// documented DEFAULT; the real bookmark carries whatever host the install page
// was served with. The loader stashes that origin on window.__recipeBookmarkletApi
// (derived from its own <script src>, so the host appears once) and the script
// above reads it back — no host hardcoded in the JS.
//
// Template (install page substitutes __BASE__ with the public base URL).
// __bccLoaderV must match LOADER_V at the top of this file:
// javascript:(function(){var p=window.open('','_blank');if(!p){alert('Pop-up blocked. Allow pop-ups for this site, then re-tap.');return;}p.document.write('<h2>Loading recipe importer...</h2>');window.__recipeBookmarkletPopup=p;window.__bccLoaderV=3;var s=document.createElement('script');s.src='__BASE__/forms/bookmarklet.js?'+Date.now();window.__recipeBookmarkletApi=new URL(s.src).origin;(document.body||document.documentElement).appendChild(s);})();
