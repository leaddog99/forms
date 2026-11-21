# Brief for the DPI session: BCC's web search and listing scorer

Written 2026-11-21 in a BCC session (repo `forms`, branch `split/enrichment-api`, commit `1d01ca5`).
For a DPI session to read, judge and plan from. Nothing in DPI was changed; DPI's code on BAILEY was
only read (file headers and comments, not the full search path).

## The question

BCC has two pieces DPI does not: a web search behind one function, and a scorer that decides whether
a store listing is the product that was named. Should DPI use them, and can they be shared as a
package instead of copied?

**Curator's direction (2026-11-21, after this brief was drafted):** BCC is still meant to migrate to
using DPI for products. But the Brave experiment produced the better search, so BCC's search and
scorer become the BASIS of DPI's search for named products. The flow is: the search moves into a
shared package, DPI builds on it, and BCC later reaches it through DPI. That settles the direction;
how it lands in DPI (the plan, the vocabularies, the governor) still belongs to the DPI session.

## What this is NOT proposing

DPI searches Amazon through Rainforest to build a category pool (`src/dpi/services/amazon_search.py`,
one credit a page). Keep that. Amazon's own search is the natural source for "what sells in this
category".

Amazon's search is the wrong tool for a different job: finding the ONE listing for a product already
known by name. It ranks the brand's bestseller, not the named product. BCC measured this on
2026-09-07 over 223 live picks: six wrong buy links, all same-brand siblings (Pastene San Marzano DOP
resolved to Pastene "Kitchen Ready" ground tomatoes; ThermoWorks ChefAlarm to the ThermoWorks DOT; a
2-piece carving set to a 6-piece set; San-J Tamari to Tamari Splash). A web search restricted to
amazon.com returned the right listing for all six, four at rank 1. BCC retired Amazon search as the
finder that day.

As far as the read went, DPI has no path for "here is a product name, find its listing": a named
product enters by link or listing id. That is the gap these pieces fill.

## Piece 1: web search

`input/pipeline/serp_search.py` (912 lines).

```python
serp_search(query, pages=7, *, want=None, gl=None, hl=None, provider=None,
            timeout=60, skip_hosts=None, should_cancel=None) -> list[dict]
# -> [{"link", "title", "rank", ...}] deduped, in result order.
#    A row on a skip_hosts domain is returned flagged {"blocked": True} and not counted toward `want`.
```

- Providers behind it: Brave Search API (active since 2026-11-21), Scale SERP, SerpApi (cancelled).
  The provider is one setting, `serp_provider`; `provider=` forces one for a single call.
- Brave: `https://api.search.brave.com/res/v1/web/search`, header `X-Subscription-Token`, key from
  the environment (`BRAVE_KEY`, also accepts `BRAVE_API_KEY` / `BRAVE_SEARCH_API_KEY`). 20 results a
  request, offset 0-9, so 200 results a query at most. One request per 1.1 s on the entry plan. A
  country or language Brave does not list answers 422; the request is repeated without them.
- Cost, estimated (the curator's actual plan was not checked): about $0.005 a request.
- Why Brave: on 2026-11-21 the Google-scraping provider returned a first page of chicken cacciatore
  for "Veal Cacciatore Recipe" three times running while Google in a browser, Bing, DuckDuckGo and
  Brave led with veal; it also cost 25-70 s of verification waits a page.

Ties to BCC, all small:

| Tie | Where | For a package |
|---|---|---|
| `system_config.get_setting` for `serp_provider`, `serp_page_retries`, `serp_page_retry_backoff`, `serp_dry_pages` | lazy imports inside four small functions | pass as arguments or a config object |
| `input.pipeline.jobs.JobCancelled` | raised when `should_cancel()` says stop | the package's own exception, or let the caller's callable raise |
| `input.pipeline.url_utils.root_domain` | one helper | move with it |
| `requests`, keys from the environment | | unchanged |

Image search (`serp_image_search`) is not wired for Brave and falls back to a Google vendor. Leave it
out of a first package.

## Piece 2: listing identity

`intake/products/curate/identity.py` (440 lines) with `intake/products/product_types.py` (95 lines).
No database, no network except the one search call. The scorer itself is pure text.

```python
google_candidates(pick, *, retailer="amazon.com", want=10) -> [{"asin", "title", "slug", "rank"}]
#   one search: "<brand> <model number> <title> site:<retailer>". The name is historical; it runs on
#   whichever provider is active.
identity_score(pick, listing_title, listing_brand="", listing_model="")
#   -> {"score", "verdict": "verified"|"weak"|"reject", "method": "model"|"title"|"brand-only"|"type",
#       "brand_ok", "why"}
best_candidate(pick, candidates) -> (candidate, score_info) | (None, None)

pick = {"product_title", "manufacturer", "model_number", "capacity", "product_class"}
```

How the score is made (thresholds: verified >= 0.6, weak >= 0.34):

1. Hard gate: shared type vocabulary (a stand mixer is not a hand mixer). Fails open when either side
   is unrecognised.
2. The manufacturer's model number in the listing is decisive (1.0 with the brand, 0.9 without).
3. Otherwise: recall of the pick title's distinctive words in the listing title, brand and filler
   stripped, units normalised.
4. Penalties: accessory or bundle words x0.5; two products sold as one listing x0.5 (a joiner
   followed by a model number); whole/ground-style form conflict x0.5; variant marker x0.8; brand
   missing x0.6 and never better than weak.

A blank result is a correct answer; a guess is not. BCC then confirms the chosen listing against the
live listing (a paid lookup) before trusting it; that second step is BCC's `verify.py` and is not
part of this proposal, since DPI already reads products through Rainforest.

Evidence, 2026-11-21:

- Brave only, 14 known products, one per class: the listing BCC already holds was found for 11, at
  rank 1 for 9, under 2 s a search, no failures. No side-by-side with the old provider was run.
- The scorer chose the held listing for 6 of the 14. Three disagreements were examined: another pack
  size of the same product; the same product under another brand's listing (BCC's own brand field
  looked wrong); and a real scorer fault, fixed the same day (commit `1d01ca5`): a model number shaped
  like `NS-ZCC10` was not recognised, and a model-number match ignored bundles and two-product
  listings. The rest were not examined.
- After the fix, BCC's audit of 260 stored picks is unchanged (218 verified, 37 weak, 5 reject).

## What is Amazon-shaped today

- `_asin_rows` and `_ASIN_IN_URL`: pull the listing id from `/dp/`, `/gp/product/`, `/clp/` addresses
  and drop any result without one.
- The localized-page handling (`/-/zh_TW/`) and the address slug read are Amazon's address shapes.

The search (`site:<retailer>`) and the scorer are store-neutral. A per-store adapter would supply:
recognise this store's product page address, return its id and the slug words. DPI's `stores`
package already has that shape. The model number is the same in every store, which is why BCC's
alternate-retailers design uses it as the join key.

## Cautions

- **Tuned on kitchen products and food.** The type groups (`TYPE_GROUPS`), accessory words, variant
  words, unit synonyms and form families are constants in code. For DPI's other categories they
  should become data, per taxonomy. That is the largest piece of real work.
- **Unproven on Brave:** BCC's review finder and publisher collection discovery. Only the listing
  search has been tested. Offer DPI the listing search only.
- **Brave returns few video links** in web results. Irrelevant to products; noted so nobody is
  surprised.
- **DPI's rules:** DPI never writes a client database; DPI-side work needs plan approval in a DPI
  session, done in `dpi_test`, with the product regression green. Nothing here asks for an exception.

## Proposed shape

One small shared package, installed by both apps, the way `llmkit` and `recipe-core` already are. Not
a copy inside DPI: the three scorer fixes of 2026-11-21 would otherwise have been made twice.

1. Search: `serp_search` and the Brave provider, settings passed in.
2. Identity: the scorer, with vocabularies as injectable data and BCC's current constants as the
   default set.
3. A store-adapter hook: `(url) -> (id, slug)` per store; Amazon's is the first.

BCC side, roughly half a day, estimated: pass the four settings in, move the three files, point BCC
at the package, keep BCC's audit (`scripts/audit_pick_identity.py`) as the regression check.

## Questions for the DPI session

1. Does DPI have, or want, a door for a product known only by name? If not, only the scorer may be
   of use (as a check on what Rainforest search returns for a named line).
2. Where would the vocabularies live in DPI: on the taxonomy instance, or on the store?
3. Should the package own the search at all, or should DPI take only the scorer and keep its own
   transports under its governor (`services/governor.py` already meters Rainforest and store APIs)?
4. Which stores first after Amazon?

## Where to read

All in the `forms` repo at commit `1d01ca5`:

- `input/pipeline/serp_search.py`: `serp_search` (line 215), the Brave provider (`_brave`, `_brave_get`).
- `intake/products/curate/identity.py`: module docstring (the 2026-09-07 audit), `identity_score`,
  `_second_product`, `google_candidates`, `best_candidate`.
- `intake/products/product_types.py`: the type groups and the fail-open rule.
- `scripts/audit_pick_identity.py`: the free regression over stored picks.
- `bcc-state-code.md`, the entries dated 2026-11-21: the day's measurements.
