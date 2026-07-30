/* Best Cooks Club — REVIEW bookmarklet.
 *
 * Run it on a product-REVIEW page (America's Test Kitchen, Wirecutter, Williams Sonoma, WSJ Buy
 * Side, …): it harvests a markdown of the page and POSTs it to /extract-review, which routes the
 * page to its per-source decoder (intake/products/review_sources), creates/updates the review
 * header + its individual product recommendations (review_products), resolves their dynamic links
 * to catalog products, and opens the Reviews editor at that review. Client-side capture, so
 * logged-in / paywalled pages work.
 *
 * Only ATK is decodable today; other recognized sources return a clear "decoder not built yet"
 * message. Loader one-liner is at the bottom. Cache-busted so edits go live on the next click.
 */
(function () {
  "use strict";
  var API = (function () {
    try { if (window.__reviewBookmarkletApi) return String(window.__reviewBookmarkletApi); } catch (e) {}
    try { var cs = document.currentScript; if (cs && cs.src) return new URL(cs.src).origin; } catch (e) {}
    try { var sc = document.querySelector('script[src*="review_bookmarklet.js"]'); if (sc && sc.src) return new URL(sc.src).origin; } catch (e) {}
    return location.origin;
  })();

  // THIS bookmarklet needs a key, unlike the recipe one. It POSTs to
  // /extract-review — a gated, edit_master endpoint — and acts immediately:
  // there is no form round-trip afterwards to supply a session. The recipe
  // grabber can be anonymous precisely because its work lands in a form where
  // the signed-in user is known; this one has no such moment.
  //
  // Baked in at install time by review_install.html from the curator's device
  // key. Without it the grab 403s, which is what it did between the endpoint
  // being gated and this being added.
  const API_KEY = (function () {
    try { return String(window.__reviewBookmarkletKey || ''); } catch (e) { return ''; }
  })();

  const _fetch = window.fetch.bind(window);
  function apiFetch(url, init) {
    init = init ? Object.assign({}, init) : {};
    // Only ever to OUR origin — never attach a credential to a publisher's host.
    if (API_KEY && String(url).indexOf(API) === 0) {
      const h = new Headers(init.headers || {});
      if (!h.has('X-BCC-Key')) h.set('X-BCC-Key', API_KEY);
      init.headers = h;
    }
    return _fetch(url, init);
  }
  var popup = window.__reviewBookmarkletPopup || null;
  function note(msg) { try { if (popup && !popup.closed) popup.document.body.innerHTML = "<h2 style='font-family:sans-serif;padding:16px'>" + msg + "</h2>"; } catch (e) {} }

  // --- compact DOM -> markdown (headings, paragraphs, lists, tables) --------
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, NAV: 1, FOOTER: 1, FORM: 1, SVG: 1, IFRAME: 1 };
  function md(node, depth) {
    depth = depth || 0;
    if (!node) return "";
    if (node.nodeType === 3) return (node.textContent || "").replace(/\s+/g, " ");
    if (node.nodeType !== 1 || SKIP[node.tagName]) return "";
    var tag = node.tagName, kids = "";
    for (var i = 0; i < node.childNodes.length; i++) kids += md(node.childNodes[i], depth + 1);
    // Keep anchors as [text](href) so the parser can read retailer buy-links + ASINs.
    if (tag === "A" && node.href) { var t = (node.textContent || "").replace(/\s+/g, " ").trim(); return t ? "[" + t + "](" + node.href + ")" : ""; }
    kids = kids.trim();
    if (!kids) return "";
    switch (tag) {
      case "H1": return "\n\n# " + kids + "\n";
      case "H2": return "\n\n## " + kids + "\n";
      case "H3": case "H4": case "H5": return "\n\n### " + kids + "\n";
      case "LI": return "\n- " + kids;
      case "UL": case "OL": return "\n" + kids + "\n";
      case "TR": return "\n| " + kids;
      case "TD": case "TH": return kids + " | ";
      case "P": case "DIV": case "SECTION": case "SPAN": case "ARTICLE": return kids + "\n";
      case "BR": return "\n";
      default: return kids + " ";
    }
  }
  function bodyMarkdown() {
    var root = document.querySelector("main, [role=main], article, #main, .content") || document.body;
    return md(root).replace(/\n{3,}/g, "\n\n").replace(/[ \t]{2,}/g, " ").trim().slice(0, 60000);
  }

  function run() {
    note("Capturing review…");
    var url = location.href, title = document.title || "";
    var markdown = ["# " + title, "*Source: " + url + "*", "---", bodyMarkdown()].join("\n\n");
    // /extract-review enqueues a `review_ingest` job and returns immediately; we poll it.
    // A big roundup takes ~30s of LLM time, and as a job the run is tracked, logged and
    // retryable instead of vanishing into a request that either worked or didn't.
    apiFetch(API + "/extract-review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: markdown, url: url })
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, status: r.status, d: d }; });
    }).then(function (res) {
      if (!res.ok) { note("Couldn’t import: " + ((res.d && res.d.detail) || res.status)); return; }
      var jobId = res.d && res.d.job_id;
      if (!jobId) {                       // sync fallback (older server): result is the review
        return openEditor(res.d && res.d.review_id, (res.d && res.d.products || []).length);
      }
      note("Reading the review… (job #" + jobId + ")");
      var tries = 0;
      var poll = setInterval(function () {
        tries++;
        apiFetch(API + "/jobs/" + jobId).then(function (r) { return r.json(); }).then(function (j) {
          if (j.status === "success") {
            clearInterval(poll);
            var out = j.result || {};
            openEditor(out.review_id, out.product_count || 0);
          } else if (j.status === "error" || j.status === "cancelled") {
            clearInterval(poll);
            note("Import " + j.status + ": " + (j.error_detail || "see job #" + jobId));
          } else if (tries > 90) {        // 3 min — the job outlives us; go look at it
            clearInterval(poll);
            note("Still running — check job #" + jobId + " in Jobs/Monitor.");
          } else {
            note("Reading the review… " + j.status + " (" + tries * 2 + "s)");
          }
        }).catch(function () { /* transient — keep polling */ });
      }, 2000);
    }).catch(function (e) { note("Failed: " + (e.message || e)); });
  }

  function openEditor(rid, n) {
    note("Imported " + n + " product rec" + (n === 1 ? "" : "s") + " — opening editor…");
    var edUrl = API + "/forms/reviews.html" + (rid ? "?review=" + encodeURIComponent(rid) : "");
    if (popup && !popup.closed) popup.location.href = edUrl;
    else window.open(edUrl, "_blank", "noopener");
  }
  run();
})();

/* ---- INSTALL ----
 * Like the product bookmarklet, the real bookmark carries the configured app host
 * (system_config.public_base_url) — nothing hardcoded here. Loader template
 * (__BASE__ substituted by the install page):
 * javascript:(function(){var p=window.open('','_blank');if(!p){alert('Pop-up blocked. Allow pop-ups, then re-tap.');return;}p.document.write('<h2>Loading review importer...</h2>');window.__reviewBookmarkletPopup=p;window.__reviewBookmarkletKey='__KEY__';var s=document.createElement('script');s.src='__BASE__/forms/review_bookmarklet.js?'+Date.now();window.__reviewBookmarkletApi=new URL(s.src).origin;(document.body||document.documentElement).appendChild(s);})();
 */