/* Enrich's own cross-page hamburger nav — the product's own chrome (NOT BCC's
 * library-shell.js). Injects a single floating menu shared by every Enrich page
 * (the test harness, the measurement admin, …) so they navigate to each other.
 *
 * Drop-in: <script src="enrich-nav.js"></script>. Self-contained (own CSS, no
 * deps). The mount prefix is derived from the URL, so it works whether the
 * service runs standalone or mounted at /enrich-api. */
(function () {
  // Service root: strip a known page suffix; default to the path minus trailing slash.
  var p = location.pathname;
  var i = p.indexOf("/measures");
  var BASE = i >= 0 ? p.slice(0, i) : p.replace(/\/$/, "");
  if (BASE === "") BASE = "";  // standalone root

  var ITEMS = [
    { key: "measures", label: "Measurements", sub: "ingredient densities & aliases", href: BASE + "/measures" },
    { key: "harness",  label: "Test harness", sub: "exercise the enrichment API",    href: BASE + "/" },
    { key: "health",   label: "Health",       sub: "service status (JSON)",          href: BASE + "/health" },
  ];
  function activeKey() {
    if (/\/measures(\/|$)/.test(p)) return "measures";
    if (/\/health$/.test(p)) return "health";
    return "harness";
  }

  var css = document.createElement("style");
  css.textContent = [
    ".enrich-hamb{position:fixed;top:11px;right:18px;z-index:9999;cursor:pointer;",
    "  border:1px solid rgba(255,255,255,.25);background:rgba(0,0,0,.18);color:#fff;",
    "  border-radius:8px;padding:6px 11px;font-size:18px;line-height:1;backdrop-filter:blur(3px)}",
    ".enrich-hamb:hover{border-color:#5db58b}",
    ".enrich-navmenu{position:fixed;top:50px;right:18px;z-index:9999;min-width:230px;",
    "  background:#1d212b;color:#e7e9ee;border:1px solid #2a2f3a;border-radius:10px;",
    "  box-shadow:0 14px 44px rgba(0,0,0,.5);display:none;overflow:hidden;",
    "  font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}",
    ".enrich-navmenu.open{display:block}",
    ".enrich-navmenu .h{padding:10px 14px;color:#9aa3b2;font-size:11px;letter-spacing:.8px;",
    "  text-transform:uppercase;border-bottom:1px solid #2a2f3a}",
    ".enrich-navmenu a{display:block;padding:11px 14px;color:#e7e9ee;text-decoration:none;",
    "  border-bottom:1px solid #2a2f3a}",
    ".enrich-navmenu a:last-child{border-bottom:0}",
    ".enrich-navmenu a:hover{background:#171a21}",
    ".enrich-navmenu a.active{color:#5db58b}",
    ".enrich-navmenu a .m{display:block;color:#9aa3b2;font-size:12px}",
  ].join("");
  document.head.appendChild(css);

  var btn = document.createElement("button");
  btn.className = "enrich-hamb"; btn.title = "Enrich menu"; btn.innerHTML = "&#9776;";

  var menu = document.createElement("nav");
  menu.className = "enrich-navmenu";
  var act = activeKey();
  menu.innerHTML = '<div class="h">Enrich</div>' + ITEMS.map(function (it) {
    return '<a href="' + it.href + '"' + (it.key === act ? ' class="active"' : "") + ">" +
           it.label + '<span class="m">' + it.sub + "</span></a>";
  }).join("");

  function close() { menu.classList.remove("open"); }
  btn.addEventListener("click", function (e) { e.stopPropagation(); menu.classList.toggle("open"); });
  document.addEventListener("click", function (e) {
    if (e.target !== btn && !menu.contains(e.target)) close();
  });

  function mount() { document.body.appendChild(btn); document.body.appendChild(menu); }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
