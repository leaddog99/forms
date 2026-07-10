/* Best Cooks Club — PRODUCT bookmarklet.
 *
 * The product-side analog of bookmarklet.js. Run it on a retailer PRODUCT page
 * (Amazon / Williams Sonoma / Sur La Table / manufacturer): it harvests the
 * schema.org Product JSON-LD + a markdown of the page, stages it (reusing the
 * recipe /stage-markdown rails), and opens the product form to extract → edit →
 * save. Client-side capture, so locked-down/logged-in pages work.
 *
 * Loader one-liner is at the bottom (paste that as the bookmark). Cache-busted so
 * edits here go live on the next click.
 */
(function () {
  "use strict";
  // A bookmarklet runs on the RETAILER's page, so location.origin is never our
  // app. The app host is NOT hardcoded here (portable-package: no data in code) —
  // it rides in on the loader, which sets window.__productBookmarkletApi and the
  // <script src> both to the configured origin (served by the install page from
  // system_config.public_base_url). We read it back from there.
  var API = (function () {
    try { if (window.__productBookmarkletApi) return String(window.__productBookmarkletApi); } catch (e) {}
    try { var cs = document.currentScript; if (cs && cs.src) return new URL(cs.src).origin; } catch (e) {}
    try { var sc = document.querySelector('script[src*="product_bookmarklet.js"]'); if (sc && sc.src) return new URL(sc.src).origin; } catch (e) {}
    return location.origin; // last resort (correct only when run on the app itself)
  })();
  var popup = window.__productBookmarkletPopup || null;

  function note(msg) { try { if (popup && !popup.closed) popup.document.body.innerHTML = "<h2 style='font-family:sans-serif'>" + msg + "</h2>"; } catch (e) {} }

  // --- harvest schema.org Product JSON-LD -----------------------------------
  function harvestJsonLd() {
    var out = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
      try {
        var p = JSON.parse(s.textContent || "");
        (Array.isArray(p) ? p : (p && p["@graph"]) ? p["@graph"] : [p]).forEach(function (o) {
          var t = o && (o["@type"] || o.type);
          if (t && String(t).toLowerCase().indexOf("product") !== -1) out.push(o);
        });
      } catch (e) {}
    });
    return out;
  }

  // --- compact DOM -> markdown (headings, paragraphs, lists, tables) --------
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, NAV: 1, FOOTER: 1, HEADER: 1, FORM: 1, SVG: 1, IFRAME: 1 };
  function md(node, depth) {
    depth = depth || 0;
    if (!node) return "";
    if (node.nodeType === 3) return (node.textContent || "").replace(/\s+/g, " ");
    if (node.nodeType !== 1 || SKIP[node.tagName]) return "";
    var tag = node.tagName, kids = "";
    for (var i = 0; i < node.childNodes.length; i++) kids += md(node.childNodes[i], depth + 1);
    kids = kids.trim();
    if (!kids && tag !== "IMG") return "";
    switch (tag) {
      case "H1": return "\n\n# " + kids + "\n";
      case "H2": return "\n\n## " + kids + "\n";
      case "H3": case "H4": return "\n\n### " + kids + "\n";
      case "LI": return "\n- " + kids;
      case "UL": case "OL": return "\n" + kids + "\n";
      case "TR": return "\n| " + kids;
      case "TD": case "TH": return kids + " | ";
      case "P": case "DIV": case "SECTION": case "SPAN": return kids + " ";
      case "BR": return "\n";
      default: return kids + " ";
    }
  }

  function bodyMarkdown() {
    var root = document.querySelector("#dp, #centerCol, main, [role=main], #productDetails, .product, article") || document.body;
    return md(root).replace(/\n{3,}/g, "\n\n").replace(/[ \t]{2,}/g, " ").trim().slice(0, 24000);
  }

  // --- harvest the product hero image ---------------------------------------
  // The DOM->markdown drops <img>, so without this the extractor only ever sees
  // an image if JSON-LD happens to carry one. og:image is the reliable hero on
  // virtually every retailer page; fall back to twitter:image / image_src / the
  // biggest visible product <img>.
  function harvestImages() {
    var urls = [], seen = {};
    function add(u) {
      if (!u) return;
      try { u = new URL(u, location.href).href; } catch (e) { return; }
      if (u.indexOf("data:") === 0 || seen[u]) return;
      seen[u] = 1; urls.push(u);
    }
    ["meta[property='og:image']", "meta[property='og:image:secure_url']",
     "meta[name='twitter:image']", "meta[name='twitter:image:src']",
     "link[rel='image_src']"].forEach(function (sel) {
      var el = document.querySelector(sel);
      if (el) add(el.content || el.href);
    });
    // Largest rendered product image as a last resort.
    var best = null, bestArea = 0;
    document.querySelectorAll("#dp img, #centerCol img, [role=main] img, .product img, article img, img").forEach(function (im) {
      var a = (im.naturalWidth || im.width || 0) * (im.naturalHeight || im.height || 0);
      if (a > bestArea && a >= 40000 && im.src) { bestArea = a; best = im.src; }
    });
    if (best) add(best);
    return urls.slice(0, 5);
  }

  function run() {
    note("Capturing product…");
    var jsonld = harvestJsonLd();
    var images = harvestImages();
    var url = location.href;
    var title = document.title || "";
    var parts = ["# " + title, "*Source: " + url + "*"];
    if (images.length) parts.push("## PRIMARY IMAGE\n" + images.join("\n"));
    if (jsonld.length) parts.push("## STRUCTURED PRODUCT DATA (JSON-LD)\n```json\n" + JSON.stringify(jsonld, null, 1) + "\n```");
    parts.push("---", bodyMarkdown());
    var markdown = parts.join("\n\n");

    fetch(API + "/stage-markdown", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: markdown, source_url: url, title: title })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.token) throw new Error("stage failed");
      var formUrl = API + "/forms/product_form.html?staged=" + encodeURIComponent(d.token) + "&url=" + encodeURIComponent(url);
      if (popup && !popup.closed) popup.location.href = formUrl;
      else window.open(formUrl, "_blank", "noopener");
    }).catch(function (e) { note("Failed: " + (e.message || e)); });
  }

  run();
})();

/* ---- INSTALL ----
 * Don't copy this by hand — the install page (/forms/product_install.html) emits
 * the loader with the configured host (system_config.public_base_url) baked in.
 * The host below is only a documented DEFAULT; the real bookmark carries whatever
 * host the install page was served with. The loader stashes that origin on
 * window.__productBookmarkletApi (derived from its own <script src>, so the host
 * appears once) and the script above reads it back — no host hardcoded in the JS.
 *
 * Template (install page substitutes __BASE__ with the public base URL):
 * javascript:(function(){var p=window.open('','_blank');if(!p){alert('Pop-up blocked. Allow pop-ups for this site, then re-tap.');return;}p.document.write('<h2>Loading product importer...</h2>');window.__productBookmarkletPopup=p;var s=document.createElement('script');s.src='__BASE__/forms/product_bookmarklet.js?'+Date.now();window.__productBookmarkletApi=new URL(s.src).origin;(document.body||document.documentElement).appendChild(s);})();
 */
