// ==UserScript==
// @name         BCC recipe capture queue
// @namespace    https://tbotb.com/bcc
// @version      0.1
// @description  Zero-click score-only path #2 — walks a queue of recipe URLs in YOUR real
//               browser (beating anti-bot for free), captures each page's JSON-LD, saves
//               to the BCC master, and self-advances with human-paced delays.
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @connect      *
// @run-at       document-idle
// @noframes
// ==/UserScript==
(function () {
  'use strict';

  // INERT unless we're in capture mode. The BCC domains page kicks off a run by opening
  // the first URL with a hash:  #bcc-capture=<apiBase>|<jobId>|<host>
  var MARK = '#bcc-capture=';
  var h = location.hash || '';
  if (h.indexOf(MARK) !== 0) return;
  var parts = decodeURIComponent(h.slice(MARK.length)).split('|');
  var API = parts[0], JOB = parts[1], HOST = parts[2];
  if (!API || !JOB || !HOST) return;

  // ---- helpers -------------------------------------------------------------
  function harvestJsonLd() {
    var out = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
      try {
        var d = JSON.parse(s.textContent);
        var arr = Array.isArray(d) ? d : [d];
        arr.forEach(function (o) {
          if (o && o['@graph']) { o['@graph'].forEach(function (g) { out.push(g); }); }
          else { out.push(o); }
        });
      } catch (e) {}
    });
    return out;
  }
  function hasRecipe(ld) {
    return ld.some(function (o) {
      var t = o && o['@type'];
      return t === 'Recipe' || (Array.isArray(t) && t.indexOf('Recipe') >= 0);
    });
  }
  function post(path, body) {
    return new Promise(function (res, rej) {
      GM_xmlhttpRequest({
        method: 'POST', url: API + path,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(body),
        onload: function (r) { try { res(JSON.parse(r.responseText)); } catch (e) { res({}); } },
        onerror: function (e) { rej(e); },
        ontimeout: function () { rej(new Error('timeout')); },
        timeout: 120000,
      });
    });
  }
  function nextHash() { return MARK + encodeURIComponent(API + '|' + JOB + '|' + HOST); }

  // ---- status banner -------------------------------------------------------
  var bar = document.createElement('div');
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#2a2018;' +
    'color:#fff;font:14px Georgia,serif;padding:8px 14px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.4)';
  function say(msg, color) { bar.textContent = '🧑‍🍳 BCC capture — ' + msg; if (color) bar.style.background = color; }
  (document.body || document.documentElement).appendChild(bar);
  say('reading this page…');

  // ---- the loop ------------------------------------------------------------
  (async function () {
    var ld = harvestJsonLd();
    // STUB / BLOCK DETECTION: a real recipe page has Recipe JSON-LD. If it's absent, we
    // very likely got a challenge/stub (rate-tripped) — back off and stop, don't hammer.
    if (!hasRecipe(ld)) {
      say('no recipe found here — looks blocked/rate-limited. Stopping; resume later.', '#7a1f1f');
      try { await post('/domains/' + HOST + '/userscript/finish', { job_id: JOB, reason: 'blocked' }); } catch (e) {}
      return;
    }
    var r;
    try {
      r = await post('/domains/' + HOST + '/userscript/capture',
        { job_id: JOB, url: location.href.split('#')[0], jsonld: ld });
    } catch (e) {
      say('could not reach BCC (' + (e.message || e) + ') — stopping.', '#7a1f1f');
      return;
    }
    var prog = (r.saved_count || 0) + '/' + (r.total || 0);
    say((r.saved ? ('saved ✓ ' + (r.name || '')) : ('skipped: ' + (r.reason || ''))) + ' · ' + prog);

    if (!r.next_url) { say('ALL DONE 🎉 — ' + prog + ' into master. Close this tab.', '#2f5a2f'); return; }

    // HUMAN PACING: random delay in [min,max] (8–25s default; 30–60s in slow mode) so a
    // burst of automated loads doesn't look botty to a rate-limiter.
    var mn = (r.min_delay || 8) * 1000, mx = (r.max_delay || 25) * 1000;
    var wait = Math.round(mn + Math.random() * (mx - mn)), left = Math.round(wait / 1000);
    var tick = setInterval(function () {
      left--; say('next in ' + left + 's… (' + prog + ' saved)');
      if (left <= 0) clearInterval(tick);
    }, 1000);
    setTimeout(function () { location.href = r.next_url + nextHash(); }, wait);
  })();
})();
