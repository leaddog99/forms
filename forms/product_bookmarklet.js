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
  var API = (location.origin && location.origin.indexOf("http") === 0)
    ? "" : "https://recipes.tbotb.com"; // same-origin when possible; tunnel otherwise
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

  function run() {
    note("Capturing product…");
    var jsonld = harvestJsonLd();
    var url = location.href;
    var title = document.title || "";
    var parts = ["# " + title, "*Source: " + url + "*"];
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

/* ---- INSTALL: paste this one line as a bookmark (opens a popup, injects this script cache-busted) ----
javascript:(function(){var p=window.open('','_blank');if(!p){alert('Allow pop-ups, then re-tap.');return;}p.document.write('<h2>Loading product importer…</h2>');window.__productBookmarkletPopup=p;var s=document.createElement('script');s.src='https://recipes.tbotb.com/forms/product_bookmarklet.js?'+Date.now();(document.body||document.documentElement).appendChild(s);})();
*/
