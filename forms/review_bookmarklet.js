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
    fetch(API + "/extract-review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: markdown, url: url })
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, status: r.status, d: d }; });
    }).then(function (res) {
      if (!res.ok) { note("Couldn’t import: " + ((res.d && res.d.detail) || res.status)); return; }
      var rid = res.d && res.d.review_id;
      var n = (res.d && res.d.products && res.d.products.length) || 0;
      note("Imported " + n + " product rec" + (n === 1 ? "" : "s") + " — opening editor…");
      var edUrl = API + "/forms/reviews.html" + (rid ? "?review=" + encodeURIComponent(rid) : "");
      if (popup && !popup.closed) popup.location.href = edUrl;
      else window.open(edUrl, "_blank", "noopener");
    }).catch(function (e) { note("Failed: " + (e.message || e)); });
  }
  run();
})();

/* ---- INSTALL ----
 * Like the product bookmarklet, the real bookmark carries the configured app host
 * (system_config.public_base_url) — nothing hardcoded here. Loader template
 * (__BASE__ substituted by the install page):
 * javascript:(function(){var p=window.open('','_blank');if(!p){alert('Pop-up blocked. Allow pop-ups, then re-tap.');return;}p.document.write('<h2>Loading review importer...</h2>');window.__reviewBookmarkletPopup=p;var s=document.createElement('script');s.src='__BASE__/forms/review_bookmarklet.js?'+Date.now();window.__reviewBookmarkletApi=new URL(s.src).origin;(document.body||document.documentElement).appendChild(s);})();
 */