# Affiliate programs + clickstream — design note

**Status:** design, nothing built. Written 2026-07-28 off the olive-oil run, which
picked Carapelli Original #1 and could only offer it at Walmart — where we earn
nothing. See the Session log of the same date in `bcc-state-code.md`.

Related: `memory/project_buy_links_revenue.md`, `memory/project_system_config.md`,
`memory/project_portable_package.md`, `memory/project_unblocker_pay_per_crawl.md`,
[docs/reviews-and-product-commerce.md](reviews-and-product-commerce.md).

---

## 1. The problem, measured

Across every offer we hold (curated picks + product records + review buy links):

| host | offers | earns today |
|---|---|---|
| amazon.com | 23 | **yes** |
| surlatable.com | 8 | no |
| williams-sonoma.com | 8 | no |
| walmart.com | 7 | no |
| nordstrom.com | 3 | no |
| madeincookware · oxo · gir.co · sears · graza | 3 each | no |
| cuisinart · cangshancutlery | 1 each | no |

**35 of 58 offers (60%) earn nothing.** Carapelli is not an edge case; it is the
common case. Amazon is simply the only network wired.

Three of those hosts — `goto.walmart.com`, `goto.target.com`, `oxo.x57o.net` — are
**Impact Radius redirectors**: somebody else's affiliate wrappers sitting in our
data. They are not merchants and must never be treated as destinations.

## 2. Where it plugs in

Two decisions already taken make this fit without redesign:

- **`buy_links.affiliate_url()` is a single chokepoint.** Today: *"Only Amazon is
  wired — every other retailer returns the clean destination unchanged."* It becomes
  table-driven; no call site changes.
- **Codes are minted at CLICK time, not storage time** (`project_buy_links_revenue`).
  Click-time minting *requires* an outbound redirect, and a redirect is exactly what
  makes clickstream capture possible. These are the same requirement, not two.

So `/go/{click_id}` logs the click, mints the code, and 302s. Stored rows keep clean
destinations, unchanged — a harvested URL stays identity-only.

## 3. Schema

```sql
-- 3.1 THE PROGRAM — one per merchant relationship.
CREATE TABLE affiliate_programs (
    name            TEXT PRIMARY KEY,   -- immutable join key: 'walmart'
    display_name    TEXT,               -- 'Walmart Creator/Affiliate'
    merchant        TEXT NOT NULL,      -- 'Walmart' — as a shopper knows it
    network         TEXT,               -- impact | cj | rakuten | shareasale | awin | amazon | direct
    status          TEXT DEFAULT 'prospect',
        -- prospect -> applied -> approved -> active -> paused | rejected | closed
        -- ONLY 'active' mints a code. Publishing an unapproved id breaches most
        -- networks' terms and earns nothing anyway.

    -- our membership
    publisher_id    TEXT,               -- our affiliate/partner id at the network
    tracking_id     TEXT,               -- per-property channel (Amazon: tracking id)
    campaign_id     TEXT,               -- the network's program/campaign id
    account_ref     TEXT,               -- account/store id, reference only
    login_url TEXT, dashboard_url TEXT, contact TEXT,

    -- how a link is built
    link_strategy     TEXT DEFAULT 'template',  -- template | amazon_tag | none
    link_template     TEXT,
    subtag_param      TEXT,             -- which param carries our click id
    supports_deeplink INTEGER DEFAULT 1,-- 0 = homepage-only program; cannot tag a product URL

    -- economics
    default_rate    REAL,               -- 0.04
    commission_note TEXT,               -- '4% most, 1% grocery'
    cookie_days     INTEGER,
    priority        INTEGER DEFAULT 100,-- tie-break when two programs can serve one product

    notes TEXT, applied_at TEXT, approved_at TEXT, created_at TEXT, updated_at TEXT
);

-- 3.2 HOSTS -> PROGRAM. The resolver: an outbound URL's host finds its program in
--     one lookup. PK on host because a host belongs to exactly one program.
CREATE TABLE affiliate_program_hosts (
    host       TEXT PRIMARY KEY,          -- 'walmart.com', matched as host or *.host
    program    TEXT NOT NULL REFERENCES affiliate_programs(name),
    kind       TEXT DEFAULT 'merchant',   -- merchant | network_redirector
    created_at TEXT
);
-- kind='network_redirector' is how goto.walmart.com / x57o.net get recognised as
-- WRAPPERS rather than merchants. Ours to unwrap; a rival's to strip.

-- 3.3 THE CLICKSTREAM.
CREATE TABLE affiliate_clicks (
    click_id      TEXT PRIMARY KEY,  -- ULID-ish; ALSO the subtag handed to the network
    created_at    TEXT NOT NULL,     -- UTC
    day           TEXT,              -- local 'YYYY-MM-DD', denormalized for grouping
    hour          INTEGER,           -- local 0-23

    -- what
    program       TEXT,              -- NULL = unmonetized; recorded anyway (see §4)
    merchant_host TEXT,
    dest_url      TEXT NOT NULL,     -- the clean destination
    final_url     TEXT,              -- what we actually redirected to
    tagged        INTEGER DEFAULT 0, -- did this click earn attribution

    -- where from ("our sources")
    surface       TEXT,              -- brief | product | recipe | cook | rail
    product_id    TEXT,
    collection    TEXT,
    slot          TEXT,              -- 'overall.1'
    page_url TEXT, referrer TEXT,

    -- who
    user_id TEXT, session_id TEXT,
    ip_hash TEXT,                    -- hashed, never raw (§7.3)
    country TEXT, user_agent TEXT, device TEXT,

    -- counted?
    counted       INTEGER DEFAULT 1, -- 0 = suppressed; the row is KEPT (§6)
    suppressed_by TEXT               -- prefetch | scanner | dedupe | ua | internal
);
CREATE INDEX idx_clk_time    ON affiliate_clicks(created_at);
CREATE INDEX idx_clk_program ON affiliate_clicks(program, created_at);
CREATE INDEX idx_clk_product ON affiliate_clicks(product_id);
CREATE INDEX idx_clk_counted ON affiliate_clicks(counted, created_at);

-- 3.4 CONVERSIONS — build later; the click_id decision below must be made NOW.
CREATE TABLE affiliate_conversions (
    conversion_id TEXT PRIMARY KEY,
    click_id      TEXT,   -- joined via the subtag we minted. This is the entire reason
                          -- click_id IS the subtag rather than a separate placement string.
    program TEXT, occurred_at TEXT, reported_at TEXT,
    order_total REAL, commission REAL, currency TEXT,
    status TEXT,          -- pending | approved | reversed
    raw TEXT
);
```

## 4. The report that answers Carapelli

Hosts appearing in offers or clicks with **no active program**, ranked by volume — a
standing worklist of which programs to join next. Today it reads Sur La Table 8,
Williams Sonoma 8, Walmart 7. This belongs on the editor's intro page.

It is also why an unmonetized click is still recorded with `program` NULL: those rows
are the demand signal. Discarding them would hide the very thing the report measures.

## 5. The `/go/` endpoint

```
GET /go/{click_id}?...   ->  302 to the tagged merchant URL
```

Minting happens here because that is where the placement, the session and the program
are all known at once. The `click_id` is generated when the LINK IS RENDERED, embedded
in the href, and the row is written when it is followed — so a rendered-but-unclicked
link costs nothing.

### 5.1 `/go/` is never challenged. Ever.

**A bot challenge on `/go/` must be permanently excluded from any WAF or bot rule.**
It sits between a reader deciding to buy and the merchant: a challenge there loses the
sale *and* the attribution, which is the worst possible placement of friction in the
system. Several networks also run automated validators against published links; a
block can fail a compliance check or lose the program outright.

Cloudflare rules on `/forms/*` and the admin endpoints are fine and close to free —
those have no legitimate automated callers. Two cautions there:

- the **bookmarklets are cross-origin XHR** from arbitrary publisher pages to
  `recipes.tbotb.com`, which is exactly the traffic pattern blunt bot rules break;
- **Pay-Per-Crawl is a policy stance, not a security setting.** We currently route
  around People Inc.'s 402 gate via the unblocker
  (`project_unblocker_pay_per_crawl`); switching it on for our own domain asserts the
  right we are bypassing. Decide it deliberately, not as part of a checkbox sweep.

## 6. Bot filtering belongs in the handler, not the perimeter

Cloudflare will not fix click quality, because what inflates affiliate clicks is
mostly not malicious:

| source | why bot scoring misses it |
|---|---|
| browser prefetch / prerender | a real browser doing a real fetch |
| link unfurlers (Slack, iMessage, WhatsApp) | declared, well-behaved, legitimate |
| corporate email security (Proofpoint, Mimecast) | visits every link in every email, presents as a real browser |
| our own monitoring | ours |

So the handler decides, in this order, and **records rather than discards** — a
suppressed click is still evidence, and a rule we cannot audit is a rule we cannot fix:

1. **Prefetch headers** — `Sec-Purpose: prefetch` / `Purpose: prefetch` →
   `suppressed_by='prefetch'`.
2. **Known scanners** — UA list for unfurlers and mail-security fetchers →
   `'scanner'`. Maintained as data, not code (`feedback_no_data_in_code`).
3. **Method** — count on navigation only; a `HEAD` is never a click.
4. **Dedupe** — same `(session_id, dest_url)` inside a short window (~30s) is one
   click, not several → `'dedupe'`.
5. **Internal** — our own IP/session → `'internal'`.

Everything else counts. `counted=1` is the default so a new, unrecognised source
shows up in the numbers rather than vanishing silently.

Independently, every affiliate link gets **`rel="sponsored nofollow noopener"`** —
required for FTC disclosure and Google's link rules, and it reduces crawler traffic as
a side effect.

## 7. Decisions worth taking deliberately

### 7.1 The Amazon settings move out of `system_config`
`amazon_tracking_id` and `amazon_store_id` are per-program membership and belong on
the Amazon program row; keeping them in `system_config` too would give two sources of
truth. `affiliate_subtag_enabled` is a genuine global toggle and stays.

### 7.2 Commission must never touch the pick
`priority` and `default_rate` may order the **offer list** — showing a monetized
retailer above an unmonetized one is honest. They must never influence **which product
ranks #1**. The moment commission moves a ranking, the authority argument the whole
system rests on collapses. Write this as an enforced boundary, not a convention: the
ranking stage should not be able to read the programs table at all.

### 7.3 Privacy
`ip_hash`, never raw IP. Set a retention window on `user_agent` / `referrer` /
`ip_hash` and delete on schedule. Clicks are revenue evidence; the identifying columns
are not, and age badly.

### 7.4 One database
Keep this in `recipes.db`: the joins to products and collections are the entire point,
the volume is small, and clicks are revenue evidence that belongs in the backup dump.
If it ever outgrows that, `ATTACH` splits it without losing the joins.

## 8. Deferred

Conversion ingestion (every network has its own report format), multi-currency,
per-category rate tables — `commission_note` carries "4% most, 1% grocery" until it
does not.

## 9. Open questions

- **Networks first or merchants first?** One Impact membership yields Walmart, Target
  and others. `network` as a column (as drafted) is simpler; a separate
  `affiliate_networks` table is more correct once several merchants share one
  membership — which, given the hosts in §1, will happen. Leaning to promoting it.
- **Is `user_id` in scope yet?** It means nothing until there are real end users;
  `session_id` alone works today and extends later at no cost.

---

## 9. Research round (2026-08-31) — the revised entity model

Curator caught the flat `affiliate_programs` table conflating STORE with
RELATIONSHIP (the 13 Shopify prospects were seeded as programs). Before
restructuring, three parallel research passes: (a) affiliate link-management
platforms' data models (Strackr, Affilimate, Skimlinks, Sovrn, Trackonomics/
impact.com, Wecantrack, Geniuslink), (b) metasearch monetization (trivago
20-F, TripAdvisor 10-K, Kayak/Booking filings, Skyscanner partner docs),
(c) content-commerce publishers (Wirecutter/NYT, Dotdash, BuzzFeed, Future
plc's Hawk). Full reports in the session transcript 2026-08-31; condensed
findings and the model they force:

### The five entities (all three sources converge)

1. **stores** — the merchant as displayed (Made In: hosts, platform=shopify,
   contact). Exists whether or not monetized. Trivago: advertiser≠property.
   Wirecutter: the pick references the product/store, never the money.
2. **networks** — Impact, ShareASale/Awin, CJ, Rakuten, Amazon Associates,
   Skimlinks/Sovrn, direct. Carries the network's MECHANICS: subid param
   name + max length + charset (Impact subId1 255 alnum · Awin clickref 50 ·
   Rakuten u1 72 · ShareASale afftrack 255 · Amazon ascsubtag restricted) —
   link building reads this metadata, never hardcodes.
3. **connections** — OUR ACCOUNT on a network (publisher id, region,
   credentials ref, dashboard). The entity the flat model missed entirely;
   Strackr and Trackonomics both model it first-class (one publisher can
   hold several accounts per network). Amazon tags = per-site tracking ids
   under one connection.
4. **programs** — the JOIN: store × connection, with status ladder
   (prospect→applied→approved→active→dead), priority, link template,
   deeplink support, commission schedule. Rates are TIME-VERSIONED data
   (trivago prices every click in advance; bids change daily) — the
   applicable rate is STAMPED ON THE CLICK, so history survives rate edits.
5. **ledgers** — immutable events only:
   - clicks: UUID minted at redirect (Skyscanner redirect_id pattern);
     the UUID IS the subid (opaque, alnum, ≤32 — the safe envelope across
     all networks; composite human-readable subids "spiral out of control"
     and are merchant-visible on some networks). Page/module/product/rate
     context lives on OUR click row, never encoded in the subid.
   - conversion EVENTS keyed (connection, network_transaction_id): each
     report row/webhook = an event (observed_at, status, amount, currency);
     reversal = a new event, NEVER an update (CJ ships corrections as
     deltas; Impact webhooks fire created/updated/reversed). Derived
     current-state view on top. click_id FK NULLABLE with match_status —
     25-30% attribution loss is structural (metasearch-measured).
   - payouts/statements: periodic aggregation with adjustment rows;
     reconcile payments-received vs approved-conversions per connection.

### Principles the research settled

- **Own click ledger = truth for behavior; network numbers = truth for
  money.** Reconcile identifier-by-identifier, never aggregates.
- **Editorial firewall (Wirecutter):** the pick record carries no retailer;
  commission rates NEVER surface in curation UIs, only in this admin.
  Routing CLICKS by earnings is normal; re-ranking PICKS by commission is
  the trust-killer. (Matches feedback_no_vendor_names + buy-links rule.)
- **Retailer priority is an offer-layer rule:** in-stock → Amazon-first
  (curator policy, Prime) → rate/EPC.
- **Amazon constraints shape the design:** /go/ must stay same-domain and
  transparent (no cloaking); displayed prices only ever from PA-API with
  ≤1h staleness (never scraped/hand-entered — don't display until PA-API);
  PA-API is GATED (3 sales/180d, revoked after 30 idle days) so degraded
  mode is the default; tags are a first-class column, not a subid.
- **Link health = two jobs** (later phase): program-status diffing from
  network merchant lists (program dead ⇒ links dead) + destination
  crawling; every link gets a fallback cascade (preferred program → other
  program same store → plain URL).
- **What NOT to build:** auctions/bidding (that's marketplace-scale), live
  price scraping, multi-touch attribution (a network negotiation, not
  infrastructure), merchant-of-record anything.

### Phasing

- **Phase 1 (now):** stores/networks/connections tables + program re-key;
  migrate amazon → connection, 13 prospects → stores; /go/ resolves host →
  store → best ACTIVE program (priority); rate stamped on click; editor
  grows Stores + Networks panes. Ledgers stay empty-compatible.
- **Phase 2 (first non-Amazon activation):** conversion-event ingestion
  (updated-since polling per connection, raw payloads kept), reconciliation
  diff report (reported-by-network vs observed-by-us).
- **Phase 3:** link-health jobs, payout/statement reconciliation.
