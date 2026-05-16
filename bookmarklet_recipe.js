// Recipe-to-form bookmarklet.
//
// 1. Stages a token on the server so a screenshot upload can land later.
// 2. Opens the form at ?url=<page>&staged=<token>.
//    - Form runs /extract-from-url first (server fetches the page, pulls
//      JSON-LD via extruct, runs the canonical markdown -> recipe call).
//    - If that fails or the recipe is incomplete (paywalled / JS-only /
//      logged-in-only content), the form auto-falls-back to the screenshot
//      this bookmarklet uploads in the background — no manual click.
// 3. In the background, captures a full-page screenshot via html2canvas
//    from the user's *already-rendered, possibly authenticated* DOM and
//    POSTs it to /stage-image/<token>.
//
// To install: copy the minified `javascript:` line at the bottom of this
// file and save it as the URL of a browser bookmark.

(async function () {
  // ====== CONFIG: pick which backend this bookmarklet talks to ======
  // For local dev: 'http://localhost:8009'
  // For remote   : 'https://recipes.yourdomain.com'  (your Cloudflare tunnel)
  const API_LOCAL  = 'http://localhost:8009';
  const API_REMOTE = 'https://recipes.tbotb.com';  // Cloudflare tunnel
  // Flip this one line to switch which target the bookmarklet uses.
  let API = API_LOCAL;
  // Mixed-content guard: HTTPS pages cannot fetch HTTP endpoints (browser
  // blocks before the request leaves; "Failed to fetch" with no further
  // detail). When the bookmarklet was built for LOCAL but the page is
  // HTTPS, silently fall over to REMOTE — both endpoints serve the same
  // API, so the user doesn't have to keep two bookmarks in their head.
  if (location.protocol === 'https:' && new URL(API).protocol === 'http:') {
    console.log('[recipe-bookmarklet] HTTPS page + HTTP API would be mixed-content; switching to REMOTE');
    API = API_REMOTE;
  }
  // ==================================================================
  const FORM = API + '/forms/recipe_form_styled.html';
  try {
    // Mint a token via /stage-markdown so /stage-image can use it.
    // The markdown body is just a placeholder — the form uses ?url= for the
    // primary extraction path and only reaches for the token when it needs
    // the screenshot fallback.
    const stageRes = await fetch(API + '/stage-markdown', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        markdown: 'URL: ' + location.href,
        source_url: location.href,
        title: document.title
      })
    });
    if (!stageRes.ok) throw new Error('Stage failed: HTTP ' + stageRes.status);
    const { token } = await stageRes.json();

    // --- 2. Open the form immediately so URL extraction can start running
    //         while we're still capturing the screenshot. ---
    window.open(
      FORM + '?url=' + encodeURIComponent(location.href) +
      '&staged=' + encodeURIComponent(token),
      '_blank'
    );

    // --- 3. Background screenshot via html2canvas. Same routine as the
    //         legacy bookmarklet — kept verbatim so paywall / logged-in
    //         pages keep working. ---
    if (!window.html2canvas) {
      await new Promise(function (res, rej) {
        const s = document.createElement('script');
        s.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
        s.onload = res;
        s.onerror = function () { rej(new Error('html2canvas failed to load')); };
        document.head.appendChild(s);
      });
    }

    // html2canvas chokes on modern CSS color functions and cross-origin
    // images. Swap them out in the cloned DOM only.
    const BAD = /\b(oklch|oklab|lch|lab|hwb|color-mix|color)\s*\(/i;
    const PROPS = ['color', 'backgroundColor', 'backgroundImage', 'borderTopColor', 'borderRightColor', 'borderBottomColor', 'borderLeftColor', 'outlineColor', 'textDecorationColor', 'fill', 'stroke', 'boxShadow', 'textShadow', 'borderImageSource'];
    const FB = { color: '#111', backgroundColor: 'transparent', backgroundImage: 'none', borderTopColor: '#ccc', borderRightColor: '#ccc', borderBottomColor: '#ccc', borderLeftColor: '#ccc', outlineColor: 'transparent', textDecorationColor: 'currentColor', fill: 'currentColor', stroke: 'currentColor', boxShadow: 'none', textShadow: 'none', borderImageSource: 'none' };
    const PLACEHOLDER = 'data:image/svg+xml;utf8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><rect width="100%" height="100%" fill="#eee"/></svg>');

    const shotPromise = html2canvas(document.body, {
      height: document.body.scrollHeight,
      width: document.body.scrollWidth,
      windowHeight: document.body.scrollHeight,
      windowWidth: document.body.scrollWidth,
      useCORS: true,
      allowTaint: false,
      logging: false,
      backgroundColor: '#ffffff',
      imageTimeout: 8000,
      onclone: function (d) {
        d.querySelectorAll('img').forEach(function (img) {
          try { const u = new URL(img.src, location.href); if (u.origin !== location.origin) { img.src = PLACEHOLDER; img.removeAttribute('srcset'); } } catch (e) { img.src = PLACEHOLDER; }
        });
        d.querySelectorAll('[style*="background-image"]').forEach(function (el) { const s = el.getAttribute('style') || ''; if (/url\(/.test(s)) el.style.backgroundImage = 'none'; });
        d.querySelectorAll('*').forEach(function (el) { try { const cs = getComputedStyle(el); PROPS.forEach(function (p) { const v = cs[p]; if (v && BAD.test(v)) el.style[p] = FB[p]; }); } catch (e) { } });
      }
    });

    const canvas = await Promise.race([
      shotPromise,
      new Promise(function (_, rej) { setTimeout(function () { rej(new Error('screenshot timed out')); }, 45000); })
    ]);

    const b64 = canvas.toDataURL('image/png').split(',')[1];
    const uploadRes = await fetch(API + '/stage-image/' + encodeURIComponent(token), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_b64: b64 })
    });
    console.log('[recipe-bookmarklet] screenshot uploaded:', uploadRes.status, '| b64 chars:', b64.length);
  } catch (e) {
    // Include the API URL so a "Failed to fetch" can be diagnosed without
    // a console — the user can tell at a glance which target was being hit.
    alert('Bookmarklet error: ' + (e && e.message ? e.message : e) +
          '\nAPI: ' + API + '\nPage: ' + location.href);
  }
})();

/*
==== MINIFIED BOOKMARKLETS (paste as bookmark URL) ====

The two versions below are identical except for the API hostname. Save them
as separate bookmarks — e.g. "Recipe LOCAL" and "Recipe REMOTE" — so you can
pick the right one depending on whether the form is loaded from localhost or
from the Cloudflare tunnel.

When your tunnel hostname changes, edit the REMOTE line below (replace
https://recipes.example.com) and re-save that bookmark.

---- LOCAL (http://localhost:8009, auto-switches to REMOTE on HTTPS pages) ----

javascript:(async function(){let API='http://localhost:8009';const API_REMOTE='https://recipes.tbotb.com';if(location.protocol==='https:'&&new URL(API).protocol==='http:'){console.log('[recipe-bookmarklet] HTTPS page + HTTP API would be mixed-content; switching to REMOTE');API=API_REMOTE;}const FORM=API+'/forms/recipe_form_styled.html';try{const stageRes=await fetch(API+'/stage-markdown',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({markdown:'URL: '+location.href,source_url:location.href,title:document.title})});if(!stageRes.ok)throw new Error('Stage failed: HTTP '+stageRes.status);const{token}=await stageRes.json();window.open(FORM+'?url='+encodeURIComponent(location.href)+'&staged='+encodeURIComponent(token),'_blank');if(!window.html2canvas){await new Promise(function(res,rej){const s=document.createElement('script');s.src='https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';s.onload=res;s.onerror=function(){rej(new Error('html2canvas failed to load'));};document.head.appendChild(s);});}const BAD=/\b(oklch|oklab|lch|lab|hwb|color-mix|color)\s*\(/i;const PROPS=['color','backgroundColor','backgroundImage','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor','outlineColor','textDecorationColor','fill','stroke','boxShadow','textShadow','borderImageSource'];const FB={color:'#111',backgroundColor:'transparent',backgroundImage:'none',borderTopColor:'#ccc',borderRightColor:'#ccc',borderBottomColor:'#ccc',borderLeftColor:'#ccc',outlineColor:'transparent',textDecorationColor:'currentColor',fill:'currentColor',stroke:'currentColor',boxShadow:'none',textShadow:'none',borderImageSource:'none'};const PLACEHOLDER='data:image/svg+xml;utf8,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><rect width="100%" height="100%" fill="#eee"/></svg>');const shotPromise=html2canvas(document.body,{height:document.body.scrollHeight,width:document.body.scrollWidth,windowHeight:document.body.scrollHeight,windowWidth:document.body.scrollWidth,useCORS:true,allowTaint:false,logging:false,backgroundColor:'#ffffff',imageTimeout:8000,onclone:function(d){d.querySelectorAll('img').forEach(function(img){try{const u=new URL(img.src,location.href);if(u.origin!==location.origin){img.src=PLACEHOLDER;img.removeAttribute('srcset');}}catch(e){img.src=PLACEHOLDER;}});d.querySelectorAll('[style*="background-image"]').forEach(function(el){const s=el.getAttribute('style')||'';if(/url\(/.test(s))el.style.backgroundImage='none';});d.querySelectorAll('*').forEach(function(el){try{const cs=getComputedStyle(el);PROPS.forEach(function(p){const v=cs[p];if(v&&BAD.test(v))el.style[p]=FB[p];});}catch(e){}});}});const canvas=await Promise.race([shotPromise,new Promise(function(_,rej){setTimeout(function(){rej(new Error('screenshot timed out'));},45000);})]);const b64=canvas.toDataURL('image/png').split(',')[1];const uploadRes=await fetch(API+'/stage-image/'+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image_b64:b64})});console.log('[recipe-bookmarklet] screenshot uploaded:',uploadRes.status,'| b64 chars:',b64.length);}catch(e){alert('Bookmarklet error: '+(e&&e.message?e.message:e)+'\nAPI: '+API+'\nPage: '+location.href);}})();

---- REMOTE (Cloudflare tunnel: recipes.tbotb.com) ----

javascript:(async function(){const API='https://recipes.tbotb.com';const FORM=API+'/forms/recipe_form_styled.html';try{const stageRes=await fetch(API+'/stage-markdown',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({markdown:'URL: '+location.href,source_url:location.href,title:document.title})});if(!stageRes.ok)throw new Error('Stage failed: HTTP '+stageRes.status);const{token}=await stageRes.json();window.open(FORM+'?url='+encodeURIComponent(location.href)+'&staged='+encodeURIComponent(token),'_blank');if(!window.html2canvas){await new Promise(function(res,rej){const s=document.createElement('script');s.src='https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';s.onload=res;s.onerror=function(){rej(new Error('html2canvas failed to load'));};document.head.appendChild(s);});}const BAD=/\b(oklch|oklab|lch|lab|hwb|color-mix|color)\s*\(/i;const PROPS=['color','backgroundColor','backgroundImage','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor','outlineColor','textDecorationColor','fill','stroke','boxShadow','textShadow','borderImageSource'];const FB={color:'#111',backgroundColor:'transparent',backgroundImage:'none',borderTopColor:'#ccc',borderRightColor:'#ccc',borderBottomColor:'#ccc',borderLeftColor:'#ccc',outlineColor:'transparent',textDecorationColor:'currentColor',fill:'currentColor',stroke:'currentColor',boxShadow:'none',textShadow:'none',borderImageSource:'none'};const PLACEHOLDER='data:image/svg+xml;utf8,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><rect width="100%" height="100%" fill="#eee"/></svg>');const shotPromise=html2canvas(document.body,{height:document.body.scrollHeight,width:document.body.scrollWidth,windowHeight:document.body.scrollHeight,windowWidth:document.body.scrollWidth,useCORS:true,allowTaint:false,logging:false,backgroundColor:'#ffffff',imageTimeout:8000,onclone:function(d){d.querySelectorAll('img').forEach(function(img){try{const u=new URL(img.src,location.href);if(u.origin!==location.origin){img.src=PLACEHOLDER;img.removeAttribute('srcset');}}catch(e){img.src=PLACEHOLDER;}});d.querySelectorAll('[style*="background-image"]').forEach(function(el){const s=el.getAttribute('style')||'';if(/url\(/.test(s))el.style.backgroundImage='none';});d.querySelectorAll('*').forEach(function(el){try{const cs=getComputedStyle(el);PROPS.forEach(function(p){const v=cs[p];if(v&&BAD.test(v))el.style[p]=FB[p];});}catch(e){}});}});const canvas=await Promise.race([shotPromise,new Promise(function(_,rej){setTimeout(function(){rej(new Error('screenshot timed out'));},45000);})]);const b64=canvas.toDataURL('image/png').split(',')[1];const uploadRes=await fetch(API+'/stage-image/'+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image_b64:b64})});console.log('[recipe-bookmarklet] screenshot uploaded:',uploadRes.status,'| b64 chars:',b64.length);}catch(e){alert('Bookmarklet error: '+(e&&e.message?e.message:e)+'\nAPI: '+API+'\nPage: '+location.href);}})();
*ok