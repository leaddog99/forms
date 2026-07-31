/* Best Cooks Club — REVIEW bookmarklet.
 *
 * Run it on a product-REVIEW page (America's Test Kitchen, Wirecutter, Williams Sonoma, WSJ Buy
 * Side, …): it harvests a markdown of the page, STAGES it, and opens the Reviews editor, which
 * runs /extract-review — routing the page to its per-source decoder (intake/products/review_sources),
 * creating/updating the review header + its individual product recommendations (review_products)
 * and resolving their dynamic links to catalog products. Client-side capture, so logged-in /
 * paywalled pages work.
 *
 * The decode runs from the EDITOR, not from here — see the note on credentials below.
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

  // NO CREDENTIAL LIVES HERE. This bookmarklet runs on the PUBLISHER's origin,
  // and the session (localStorage `app:session_token` + X-Self-User-Id, attached
  // by library-shell's fetch patch) belongs to OUR origin — a bookmarklet can
  // never read it. That is a property of the browser, not an omission.
  //
  // So there are only two possible designs, and this one used to be the wrong
  // half: it POSTed straight to /extract-review carrying a baked device key,
  // which meant a second identity system existing solely to skip the hand-off,
  // and a ~$0.29 LLM job authorised by a token sitting in a bookmarks bar with
  // no human in front of an editor. Now it does what the product grabber does:
  // stage the worthless part anonymously, then navigate to reviews.html, where
  // the session exists and the editor spends. Identity before spend — the same
  // ordering the recipe grabber learned on 2026-07-30.
  //
  // A stale install that still sets window.__reviewBookmarkletKey is harmless:
  // nothing reads it. No re-install needed.
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
    // Anonymous by design: /stage-markdown accepts a grab from anyone because staged
    // content is transient and worthless until someone signed in decides to spend on
    // it. Nothing is decoded, no job is enqueued and no money is spent here.
    fetch(API + "/stage-markdown", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: markdown, source_url: url, title: title })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.token) throw new Error("stage failed");
      note("Opening the Reviews editor…");
      // Hand off to our own origin. reviews.html reads ?staged, POSTs /extract-review
      // with the curator's session, polls the job and opens the finished review — the
      // same handshake products.html already uses for the product grabber.
      var edUrl = API + "/forms/reviews.html?staged=" + encodeURIComponent(d.token) +
                  "&url=" + encodeURIComponent(url);
      if (popup && !popup.closed) popup.location.href = edUrl;
      else window.open(edUrl, "_blank", "noopener");
    }).catch(function (e) { note("Failed: " + (e.message || e)); });
  }

  run();
})();

/* ---- INSTALL ----
 * Like the product bookmarklet, the real bookmark carries the configured app host
 * (system_config.public_base_url) — nothing hardcoded here, and NO key: the loader
 * stopped baking one on 2026-07-31 when the decode moved into the editor. Existing
 * installs that still set window.__reviewBookmarkletKey keep working; the payload
 * ignores it. Loader template (__BASE__ substituted by the install page):
 * javascript:(function(){var p=window.open('','_blank');if(!p){alert('Pop-up blocked. Allow pop-ups, then re-tap.');return;}p.document.write('<h2>Loading review importer...</h2>');window.__reviewBookmarkletPopup=p;var s=document.createElement('script');s.src='__BASE__/forms/review_bookmarklet.js?'+Date.now();window.__reviewBookmarkletApi=new URL(s.src).origin;(document.body||document.documentElement).appendChild(s);})();
 */