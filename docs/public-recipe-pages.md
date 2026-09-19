# Public recipe pages + JSON-LD — getting OUR recipes into Google

**Status: DESIGN (2026-09-19). Nothing built.** Supersedes nothing; it implements the
JSON-LD ruling of 2026-08-10 (state log, "the product thesis gets named") and the
"SCOPE/REMOVE per path when real public pages ship" note on the no-index shield in
`save_recipe_api.py`. Related: [user-as-publisher-rights.md](user-as-publisher-rights.md)
(the rights model), memory `project_image_policy`, `project_discover_vs_possess`.

> **Not legal advice.** Same caveat as the rights doc: this records a defensible SHAPE.

---

## 1. The ruling this builds on (2026-08-10, unchanged)

* **An extracted recipe never gets `Recipe` markup.** Rich results need
  `recipeIngredient` + `recipeInstructions` — exactly what the teaser bound withholds —
  and it would put us in the SERP competing with the publisher we send traffic to.
  Extracted/ranked content gets **`ItemList` + `Review`** ("the best versions, ranked,
  and why").
* **A recipe WE author gets full `Recipe` markup, legitimately.**
* Law as checked then: ingredients are facts, method is functional, headnote and the
  specific wording are protected expression, photos are protected. The constraint was
  never legal, it was **supply**: every stored `recipeInstructions` is the publisher's
  verbatim prose; only a cook-reworked recipe carries OUR expression.

Curator, 2026-09-19: *"we might limit it to copies of our OWN recipes… from copies of our
extracts."* That is the same line, and it names the mechanism: **a copy**.

## 2. What exists today (measured 2026-09-19)

| | |
|---|---|
| JSON-LD emitted anywhere | **none** |
| `/r/<id>` | a 302 into `recipe_form_styled.html` — a JavaScript editor; a crawler sees a shell |
| Indexing | **shielded on purpose**: middleware stamps `X-Robots-Tag: noindex, nofollow` on EVERY response; `/robots.txt` = `Disallow: /` |
| Reworked recipes (`_cook` present) | **53** (18 master, 35 personal) — all 53 pass their validators, all 53 carry OUR `headnote` |
| …whose hero image is ours (generated / uploaded) | **20** |
| …showing the publisher's photo | **32** (18 hot-linked URLs, 14 local `og-thumbs` copies) + 1 with none |
| Authored / handwritten (no third-party source) | 33 master, 63 personal |

Two facts drive the design:

1. **`_cook` is a complete second recipe in our words** — `headnote`, `ingredients`,
   `steps[].name/instruction`, `tips`, `cooks_note`, `recipe_yield`, `total_time_minutes`.
   The expression exists. 52 of 53 have `{bundle:…}` tokens in step text that must be
   rendered to plain prose before anything public sees them.
2. **The image is the second gate, and it is tighter than the text.** Google REQUIRES
   `image`; ours must be one we have rights to. `project_image_policy`: a publisher photo
   is an attributed thumbnail at most, never the hero of a page we claim as ours.

## 3. Google's requirements (developers.google.com/search/docs/…/recipe, read 2026-09-19)

* **Required:** `name`, `image`. **Recommended:** `author`, `datePublished`, `description`,
  `recipeIngredient`, `recipeInstructions` (`HowToStep`), `recipeYield`, `totalTime` or
  `prepTime`+`cookTime` (the pair only together), `recipeCategory`, `recipeCuisine`,
  `keywords`, `nutrition.calories`, `aggregateRating`, `video`.
* **Image:** crawlable and indexable; "must represent the marked up content"; ≥ 50K
  pixels; supply **16x9, 4x3 and 1x1**.
* **Instructions:** instructional text only — no "Step 1", no "Directions", no metadata.
* **Lists:** an `ItemList` needs a summary page that lists every recipe in it.
* The markup must describe what is **visible on the page**. That is why §5 is a real
  server-rendered page and not a tag bolted onto the editor.

## 4. The entity: a PUBLIC EDITION (the "copy")

A public edition is a **separate, frozen record** derived from one recipe — not a view of
it. The extract stays private and editable; the edition is what the world sees.

New table `public_editions` (SQL columns, not JSON digging — `feedback_persist_derived_values`):

| column | |
|---|---|
| `slug` PK | `chicken-milanese` — cleaned by `naming.clean_key_name`, unique, immutable once published |
| `recipe_id`, `source_table` | the recipe it was cut from |
| `source_url`, `source_host` | the page it is adapted from (NULL = authored) |
| `status` | `draft` → `approved` → `published` → `withdrawn` |
| `payload` | JSON: exactly the fields the page renders (§5). Nothing else is ever public |
| `jsonld` | the stored `Recipe` block, built ONCE at publish |
| `builder_version`, `cook_version` | which code and which rework produced it |
| `image_path`, `image_origin` | `generated` \| `uploaded` — never a publisher URL |
| `eligibility` | JSON snapshot of the gate (§6) at publish time — auditable later |
| `approved_by`, `approved_at`, `published_at`, `withdrawn_at`, `withdrawn_reason` | |

Why a copy and not a flag on the recipe:
* **The teaser bound stays intact by construction.** A renderer that can only read
  `payload` cannot leak the publisher's instructions, headnote or photo, whatever the
  recipe row holds.
* **Re-extraction cannot silently change a public page.** A dish refresh force-refreshes
  extracts nightly; the edition changes only when someone republishes.
* **Withdrawal is one row** (DMCA, a publisher's objection, our own second thoughts), and
  it leaves an audit trail.
* `project_discover_vs_possess` holds: nothing flows from a user's library into the public
  corpus. **Master recipes only in v1**; a personal recipe needs its owner's explicit
  publish action (the rights doc's model) and is out of scope here.

## 5. The page

`GET /recipes/<slug>` — **server-rendered HTML**, public host only (`host_gate`), page
shell per `feedback_page_shell_contract` (library-shell.css, tokens.css last, shared
header), strings through `t(key)` (`feedback_i18n_as_we_go`), no vendor names.

Renders, from `payload` only:
* name · OUR headnote · our image (three crops) · yield · total time
* **ingredients composed from `_cook`** (name + the amounts the steps carry), NOT the
  publisher's `recipeIngredient` strings. The quantities are facts; the phrasing
  ("6 chicken thighs, (about 1 1/2 pounds)*") is theirs. Composing from `_cook` keeps the
  whole page in one voice and removes the question.
* steps: `name` + `instruction` with `{bundle:…}`/token syntax rendered to plain prose;
  tips and cook's note where present
* **"Adapted from <Publisher> — <their title>"**, a real, visible, followed link, above
  the fold. Attribution is the relationship, not a footnote.
* "Cook this hands-free" → the cook view (the member hook; §1's conversion moment is
  capture, and it sits upstream of the click-out)
* equipment → product links via the existing click-time minting (`project_buy_links_revenue`)

`/r/<id>` is unchanged (it is the editor's permalink). A published master recipe's editor
view shows its public URL; the public page never links back into the editor.

## 6. The eligibility gate — ALL must hold, checked in code, snapshotted on the row

1. `_cook` present, `validators.passed`, and the rework is current for the recipe text.
2. OUR `headnote` non-empty. The publisher's `description` is never used.
3. **An image we own** (`generated` or `uploaded`). No hot-link, no `og-thumbs` copy.
4. Rendered steps contain no unresolved `{…}` token and no "Step N" prefix.
5. Every `recipeIngredient` line has a name; to-taste items say so.
6. `name` is a dish (Google: "facial scrub" is not). Reuse the is-recipe/dish identity.
7. Source publisher is **not excluded** (§9, decision C): a per-domain flag on `domains`
   (`feedback_no_data_in_code`), default per the curator's ruling.
8. A human approved it (`approved_by`). v1 publishes nothing automatically.

Failing any → the recipe simply has no edition. **Everything without an edition stays
no-indexed**, exactly as today.

## 7. The JSON-LD

Built by one function, `build_recipe_jsonld(payload) -> dict`, stored on the row, emitted
verbatim in `<script type="application/ld+json">`. No compute-on-read.

```json
{ "@context": "https://schema.org", "@type": "Recipe",
  "name": "Chicken Milanese",
  "image": ["…/1x1.jpg", "…/4x3.jpg", "…/16x9.jpg"],
  "author": {"@type": "Organization", "name": "Best Cooks Club", "url": "https://…"},
  "datePublished": "2026-10-01",
  "description": "<our headnote, trimmed>",
  "recipeYield": "4 servings", "totalTime": "PT45M",
  "recipeCategory": "Main", "recipeCuisine": "Italian", "keywords": "…",
  "recipeIngredient": ["4 chicken breasts, pounded thin", "…"],
  "recipeInstructions": [{"@type": "HowToStep", "name": "Mix the seasoned salt",
                          "text": "In a small bowl, stir together…", "url": "…#step-1"}],
  "isBasedOn": "https://cooking.nytimes.com/recipes/1024229-chicken-milanese" }
```

Deliberate omissions:
* **`aggregateRating`** — the stored ratings are the PUBLISHER'S readers'. Claiming them on
  our page is false, and Google penalises it. Ours when we have our own.
* **`nutrition`** — only if we computed it; never copied.
* **`prepTime`/`cookTime`** — Google wants the pair or neither; `_cook` has a total only.
* `isBasedOn` is honest provenance in the markup, matching the visible "Adapted from".

A validator (`validate_recipe_jsonld`) runs at publish and in a regression check
([[project_regression_check_cycle]]): required fields, ISO-8601 durations, image
dimensions ≥ 50K px and all three ratios present, no `{`, no "Step ". Publish fails loudly.

## 8. Indexing — lift the shield per PATH, never globally

* Middleware: `X-Robots-Tag: noindex, nofollow` on everything EXCEPT an allow-list of
  public prefixes: `/recipes/`, the three image crops, (later) `/dishes/` and `/sitemap.xml`.
  The allow-list is code (it is a security boundary), the editions are data.
* `/robots.txt`: `Disallow: /` then `Allow:` the same prefixes. Cloudflare's managed
  robots file currently overrides ours at the edge — reconcile it there.
* `GET /sitemap.xml`: `published` editions only, `lastmod` = `published_at`.
* A withdrawn edition answers **410 Gone** and drops from the sitemap.
* Only on the real public domain. `bcc_link_domain` is currently the TEMPORARY tunnel host
  (`project_bcc_link_domain_override`); nothing public ships on `recipes.tbotb.com`.

## 9. Decisions for the curator

| | Question | Recommendation |
|---|---|---|
| **A** | Do master editions exist at all, or authored-only? | Yes to reworked masters — that IS the 08-10 "recipes we author" once the method, headnote and image are ours — but see B and C. |
| **B** | How many, how fast? | **Tens, hand-approved, not thousands.** Google's spam policy names "scaled content abuse": mass-producing pages by transforming others' content. Fifty well-made pages with a tested cook view are an asset; five thousand auto-reworks are a liability to the whole domain. |
| **C** | Which publishers may be adapted? | Start with **ungated** publishers only. A reworked NYT Cooking or ATK recipe is lawful on the facts, but it is the sharpest edge of "competing with the publisher we link to", and they are the ones who would notice. Flag on `domains`. |
| **D** | Generated images as the hero of a Google result? | Allowed, and ours. Google requires the image to "represent the marked up content" — a generated plate of the dish does; label it as an illustration on the page. Real photos from the curator's kitchen beat it whenever they exist (`uploaded`). |
| **E** | Which domain? | bestcooksclub.com, after the transfer. No public edition before then. |
| **F** | Ingredient lines from `_cook` or from the source? | From `_cook` (§5). One voice, no question to answer. |

## 10. Supply and cost

53 reworked → 20 with an owned image → minus gated publishers (C) → a first cohort of
roughly **a dozen**. The markup is a day's work; **inventory is the constraint.** Each new
edition costs one cook-rework (economics in the rights doc §6) plus one image
(gpt-image-1 "standard" ≈ $0.04, three crops from one render). Choose WHAT to rework from
demand — the dish keyword corpus — not from whatever was reworked for other reasons.

## 11. Phases

* **P0** — curator rules on §9. No code.
* **P1** — `public_editions`, the gate, token rendering, `build_recipe_jsonld` +
  validator, the server-rendered page. **Shield still fully on**: previewable by staff,
  invisible to Google. Proves the page and the markup with Google's Rich Results Test on a
  tunnelled preview.
* **P2** — the image path: owned hero required, three crops, stored under a public prefix.
* **P3** — on the real domain: path-scoped shield, robots, sitemap, Search Console; publish
  the first hand-approved cohort; watch coverage and rich-result reports for two weeks
  before adding more.
* **P4** — dish pages with `ItemList` + `Review` (the 08-10 plan for everything we do NOT
  author), listing our editions where they exist and linking out where they do not.

## 12. Risks, stated plainly

* **Publisher relations.** We rank and link to these sites; a public adaptation can read as
  taking. Mitigations: prominent followed attribution, ungated publishers first, a one-row
  withdrawal, and a standing offer to withdraw on request.
* **Scaled-content policy** (B). Volume discipline and real added value — the tested,
  attention-aware method is the difference between an adaptation and a spin.
* **Thin/duplicate content.** Our page must be better for a cook than the source, or it
  should not exist. The cook view, tips and equipment are that difference.
* **Image truthfulness.** A generated image that misrepresents the dish fails Google's
  rule and the reader. Review every hero at approval.
* **Drift.** The edition is frozen; the rework may improve. Republishing is explicit and
  bumps `cook_version` — never silent.

## 13. LLM readers (ChatGPT / Claude / Perplexity browsing) — same fix, one warning

Tested 2026-09-19 with a plain fetch, no JavaScript, no login, against a reworked recipe:

| what was fetched | result |
|---|---|
| `/r/<id>` (the page a person opens) | 433 KB of HTML, **zero recipe content** — name and ingredients appear only after JavaScript runs. An assistant's fetch tool sees an empty editor. |
| `/robots.txt` | `Disallow: /` — a well-behaved assistant will not fetch us at all |
| `GET /recipes/<id>?user_id=0` (the JSON API) | **HTTP 200, the full recipe, all 6 instructions, no login** |

So the curator's premise is right for PAGES and wrong for DATA:

* **Pages:** an assistant cannot read us, for the same reason Google cannot. The
  server-rendered public edition (§5) fixes both at once — visible HTML plus JSON-LD is
  exactly what assistants and answer engines parse. No separate "LLM version" is needed.
  If we want to be cited, allow the assistants' crawlers on the public prefixes in
  robots.txt (OAI-SearchBot, ClaudeBot, PerplexityBot, Google-Extended are separately
  addressable) — a curator decision, same allow-list as §8.
* **Data — THE WARNING:** `noindex` and robots.txt are requests, not locks. The JSON API
  on the tunnel host answers anyone who has a URL with the publisher's verbatim method —
  the very content the teaser bound exists to withhold. The teaser is enforced in the
  browser, not at the API. Before ANY path becomes public and crawlable (§8), the API
  needs a real gate: the public host serves only `public_editions` payloads, and the
  full-record endpoints require a session (or sit behind Cloudflare Access, as the shield
  comment in `save_recipe_api.py` already suggests). Treat this as a P1 prerequisite, not
  a P3 nicety.
* A deliberate assistant integration (an MCP server or a documented read API over the
  public editions) is a later option; it would serve the SAME payloads, never the corpus.
