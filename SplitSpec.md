spliSpec: Separate the codebase into two products (do this FIRST)
For: Claude Code, operating in the BestCooksClub repository it authored. Why first: the two products have opposite risk/needs; entangling them is the danger (local tool = "we possess nothing, user-driven"; corpus = "our curated content product"). Separate cleanly now, then evolve each half on its own. You know this repo — apply the classification and constraints below to the actual modules/tables/endpoints; do not wait for a file list.

Hard rule that overrides convenience everywhere below: data flows one direction only — Corpus → (read/subscription) → Local. NEVER user-sourced content → Corpus. Any existing "promote to master" path that originates from user captures must be cut. Promotion to master originates ONLY from our own editorial/crawl process.


Phase 1 — Inventory & classify (you run it; the user approves the map)
Walk the repo and assign every module, table, endpoint, job, and config to exactly one bucket. Produce a written map and stop for the user's approval before moving code.

Bucket L — Local engine (Company A, "the tool"):

recipe rework pipeline's deterministic parts: render, validators/gauntlet, bundling, scheduling, unit conversion, the ingredient views (shopping/in-order/bundles)
capture (bookmarklet/clipboard/photo) and its parsing
the user's recipe store (their RecipeDocs), and anything that reads/writes it
the LLM judgment calls (extract/anchor) AS CALLS — the prompts/backends, not the corpus

Bucket C — Corpus / reviews (Company B, "the content product"):

dishes table + matching, cohort/embedding logic, scoring (popularity/exceptionalism/ authority), editorial fields
the master corpus store, the work-product cache, "promote to master," ranking, discovery
any crawl/ingest that builds the corpus from OUR sources (not from users)

Bucket S — Shared core (a library both depend on, neither owns):

the rules/spec themselves (the rework procedure, the gauntlet definitions) — the real "engine" is these rules
the RecipeDoc / data models and serializers
pure utilities with no business allegiance

Ambiguity rule: if a piece could be L or C, default it to S only if it is pure logic with no data allegiance; otherwise flag it in the map for the user to decide. Do NOT guess on anything that touches a data store — surface it.

Deliverable of Phase 1: a markdown map (module/table/endpoint → L|C|S, with the flagged ambiguous items called out and a one-line reason each). Present it; wait for approval.


Phase 2 — Extract the shared core (S) as an installable package — ONE copy, imported by both
The shared core must live in exactly one place and be imported by both products — never copied into each. One copy = nothing to drift; "stays the same" is guaranteed by there being only one. Do NOT use a bare shared/ folder on sys.path (breaks on packaging/deploy/ repo-split); make it a real package from the start.

Create a standalone package bcc-core (own pyproject.toml) containing Bucket S: the rules/spec, RecipeDoc + models + serializers, pure utils. No imports from L or C. No DB access. No network.
Now (single repo, dev): both products consume it via editable install (pip install -e ../bcc-core or path dependency). Edit the core once; both products see it immediately, always identical. This is the build-up step — do it properly; never duplicate the core into each product.
After the repo split (Phase 7): bcc-core becomes its own repo/package, and each product depends on it by pinned version (private package index or pinned git ref). This gives the two businesses independent release cadence (Company A can adopt a new core version when ready; B can lag) and makes them genuinely separate consumers of a shared, versioned artifact rather than one codebase in two coats.
Trade to choose deliberately: editable install = always byte-identical (best for dev); pinned versions = independent/deliberate adoption (best for two deployed companies). Start with editable; move to pinned at the split.
Gate: bcc-core has zero imports of L/C modules and zero I/O; L and C each import it as a dependency (not a copy); no second copy of the rules/RecipeDoc exists anywhere.

Ownership question for counsel (entity split): bcc-core is the one thing legitimately shared between the two companies. Decide who owns it and how each company consumes it (e.g., Company A owns and licenses to B; jointly held in a third entity; etc.). Flag to the same attorney handling the corporate separation — it is part of making the two entities genuinely separate.


Phase 3 — Split the product code into two deployables (cull-down is fine — with a gate)
The shared core is already extracted (Phase 2), so do NOT duplicate it here. For the product code, either build-up (move pieces into clean targets) or cull-down (duplicate the repo into Local and Corpus, then delete the other bucket from each) is acceptable. Cull-down keeps both runnable throughout and is often faster solo — but it has one failure mode: incomplete deletion ("it still runs" because nothing imported the leftover) which leaves foreign-bucket code/tables behind. That residue is exactly what undermines the separation, so cull-down is allowed ONLY with the static-analysis gate below.

Local product (Company A) keeps Bucket L; Corpus product (Company B) keeps Bucket C. Each depends only on bcc-core, never on the other product.
Whichever method: no cross-imports L↔C. Contact only via the explicit read API (Phase 5).
Gate (static analysis, not "it runs"): prove via grep/dead-code/unused-import/orphaned- module analysis that no foreign-bucket code survives in either repo — used OR unused (no leftover C modules/endpoints/table defs in Local; no leftover L capture/user-store code in Corpus). "It runs" is NOT sufficient; absence must be proven.


Phase 4 — Separate the data stores (the load-bearing step)
Two distinct datastores. The local store (user RecipeDocs) and the corpus store (master/cache/scoring) must be physically separate — different databases, no shared tables, no shared connection. The local product cannot read corpus tables; the corpus product cannot read the user store.
One-directional boundary, enforced in code: the ONLY way the local product gets corpus data is a read call to a Corpus API (a CorpusReadClient in L that calls C); the ONLY way anything enters the corpus is C's own editorial/crawl path. There is no write endpoint on the corpus that accepts user-sourced content. Remove any such path if it exists.
Cut the user→master pipe. Audit "promote to master": its input must be C's own sources, never a user capture. If the current code lets a user capture become corpus content, delete that flow and replace promotion with an editorial-only origin.
Gate: (1) no shared DB/handle between L and C; (2) the corpus exposes read-only to clients; (3) there exists no code path by which a user capture reaches the corpus store.


Phase 5 — Define the only permitted connection (subscription = read access)
The allowed relationship: a Local user may subscribe to the Corpus to read curated recipes. Money/entitlement flows up; corpus work product flows down; no content flows up.
Implement as: CorpusReadClient in L authenticates the user's subscription and reads master recipes / cache hits. It has no write capability. Subscribers consume; they never feed.
Gate: the subscription path is read-only; there is no API surface for a subscriber to contribute content to the corpus.


Phase 6 — Verify the separation is real, not cosmetic
Checklist (all must pass; these guard against the alter-ego / "one enterprise" failure):

bcc-core is pure (no I/O, no L/C imports); both sides depend on it.
No import crosses L↔C; each deployable runs alone.
Two physically separate datastores; neither product can reach the other's tables.
Corpus is read-only to clients; the only write path is C's own editorial/crawl.
No code path carries a user capture into the corpus store (the one-directional rule).
Subscription = read access only; subscribers cannot contribute content.
L can fully build/run/test with C absent (it just loses corpus reads); C can build/run/ test with L absent.


Phase 7 — Promote the proven-clean halves to two repos (the two companies)
Only after Phases 1–6 are green (separation proven clean inside the one repo) lift each half into its own repo. Because there are no cross-imports left, this is mechanical, not surgical.

Local product → its own repo (Company A). Corpus product → its own repo (Company B).
bcc-core → its own repo/package; each product now depends on it by pinned version (switch from editable install to a pinned dependency).
Two separate repos is the destination (cleanest for the two-company / non-alter-ego story); a monorepo with three packages is acceptable but blurs the "separate companies" narrative.
Gate: each repo builds and tests from a clean checkout depending only on a pinned bcc-core; neither repo references the other; datastores remain physically separate.


After separation (do NOT start until Phases 1–6 are approved & green)
Company A / Local: evolve toward the local-first build (see LOCALIZATION-SPEC.md): local data the user controls, one outbound LLM call, optional corpus subscription. iOS/ packaging decisions handled there.
Company B / Corpus: stays the Python/server brain — dishes table, matching, scoring, cache (work-product only, batch refresh), editorial promotion to master.

Legal/structural note (not legal advice): this code separation supports a corporate separation (two entities) discussed with counsel. Keeping the data stores and write paths genuinely separate — no shared DB, one-directional content flow, arm's-length read API — is what makes the separation real rather than an org-chart fiction. Don't let a later "just share this one table" convenience collapse it.


---

# Display & Data Model (settled 2026-06-05)

This section is the canonical data-and-display contract for the Corpus product. Phase 1's field classification and Phase 4's datastore split fall straight out of it. Read it before touching any corpus table or any endpoint that emits corpus data.

## The product identity — a vertical search engine with significant value-adds

The Corpus is **not a recipe destination**. It is a **vertical search engine**: it indexes the world's recipes, judges which are best, and points you to the source. The "value-adds" — editorial quality ranking (exceptionalism, not just popularity), dish-level organization, critique, "how to make it better" tips, and cross-source synthesis ("the 5 best Pastitsios, ranked") — are the **transformation** that makes us more than an index. Transformation is also what hardens the legal posture: we ingest to index and rank, and emit only our own work plus facts-and-a-link.

The two products in one line:

> **Corpus = discover & judge** (the search engine that tells you which recipes are best and points you to them). **Local = possess & use** (where the user keeps and cooks the copy he chose to save). The subscription is the referral: the engine sends you to the recipe; keeping it to cook is the local tool, *your* copy.

**Discover-vs-possess is the whole cleavage.**

Everything below is just "what a search engine does," applied to our data.

## The overriding rule (restated for this section)

Content flows **one direction only: Corpus → (read) → Local.** Source-expressive content additionally flows **out of nothing** — not to our own public pages, not to subscribers. The corpus emits only our work product + facts + a link. A user's saved recipe never travels back up to the corpus.

## Three-tier field partition (every corpus record)

A corpus record (master recipe / index row) has three classes of fields. The boundary that matters is not only "user content can't flow up" — it's also **"source-expressive content can't flow out, only our work product can."**

| Tier | Examples | Stored? | Emitted (public page OR subscriber read)? |
|---|---|---|---|
| **Source-expressive** | full ingredient list w/ quantities, step-by-step methods, source long-prose description, the raw Recipe JSON-LD blob | **Yes** — corpus-internal, for search/match/score | **Never.** Not to our pages, not to subscribers. |
| **Our work product** | critique, "make it better" tips, scores (OU/PA/DA/exceptionalism), ranking, identity card, classification, embedding, our own blurb | Yes | **Yes** — this is the *only* expressive thing the corpus serves. |
| **Keys / provenance** | normalized URL, domain, friendly name, capture/refresh dates | Yes | Yes (pointers + attribution) |

## The emit/seal cut + a single whitelist serializer

The load-bearing rule: **the corpus's only output — to its own display AND to the subscription read API — is the work-product tier + the keys tier + the rich-result envelope (below). The source-expressive tier is sealed: it crosses no boundary, ever.**

Enforce with **one chokepoint**: a `corpus_public_view(record)` serializer that emits *only* an explicit allow-list of work-product / envelope / key fields. **Whitelist, never blacklist** — a blacklist means the day someone adds a new field carrying source text, it silently leaks. This is the same muscle `recipe_model.py` already has (`static_subset` / `USER_TOP_LEVEL_FIELDS`); we extend it to a third partition and make `corpus_public_view()` the *only* path data leaves the corpus.

## The rich-result envelope (the safe display surface)

The display whitelist gets a crisp, near-machine-checkable definition:

> **Corpus display = our work product ∪ the schema.org rich-result-eligible field set, and every item carries a mandatory outbound source link.**

The envelope = the fields publishers emit in their schema.org `Recipe` JSON-LD *specifically so search engines syndicate them and link back*: title, thumbnail, `aggregateRating`/`reviewCount`, `prepTime`/`cookTime`/`totalTime`, `recipeYield`, nutrition summary, author/publisher. These are facts + a thumbnail-and-link — the exact bargain the publisher opted into.

The operational test for "doesn't smell of a replacement recipe":

- **Forbidden out:** full ingredient list *with quantities*, sequential step instructions — anything that lets a user cook the dish without visiting the source.
- **Allowed out:** scores, rankings, "the 5 best X → [links]", and critique/tips that *reference* ingredients as commentary ("the béchamel-to-pasta ratio is the lever"). Referencing an ingredient to critique it is commentary; reproducing the list is republication.
- **The clean test:** could a user reconstruct and cook the dish from corpus output alone? If yes, the firewall leaked.

**Description prose is ours, not theirs.** Google snippets the publisher's `description`, but that's publisher-authored expression — we render *our* blurb/critique as the prose and treat the publisher's description as sealed (or at most a short attributed snippet).

**Image is thumbnail-and-link, not co-opt.** The local/user image-coopt policy (host our own copy) is for the *user's private device*. The corpus *public* display uses a small thumbnail that links back — the Google posture, a different rights stance than the user's personal copy.

## Mandatory source link (a hard gate)

No master display item renders without a working outbound source link. The link is not decoration — it is what makes us an index/pointer rather than a destination, and it is the "always send the user to the real recipe" guarantee. **`corpus_public_view()` refuses to emit a record with no resolvable source URL.**

## Index, not cache

Call the corpus store an **index**, not a cache. "Cache" implies a convenience copy of someone else's thing and invites the "just serve it down" bridge. "Index" names it correctly: it is the core data structure of a search engine, and storing page text to rank it is what indexes *do*. The reframing kills the bridge temptation by construction.

## Raw JSON-LD = sealed provenance

Keep the raw Recipe JSON-LD blob, but understand its job and its tier:

- **Job:** it is the timestamped *receipt* of what the publisher chose to syndicate (best evidence for the rich-result fields), plus a cheap backfill source (add a field to the envelope later → re-derive from the blob, no re-crawl) and a drift source (diff a fresh crawl against the stored blob → feed `source_changed_at`).
- **Tier:** **sealed.** The Recipe JSON-LD *contains* `recipeIngredient` and `recipeInstructions`, so the blob carries the full body. It lives sealed; only the *extracted envelope fields* are emittable. Never "we saved the JSON-LD, just serve it down."
- **Implementation:** persist the blob you already parse at capture time. Keep the `Recipe` block (and `Organization`/`Publisher` for attribution); pages often ship multiple JSON-LD blocks — don't keep the whole pile.

## Fact-vs-expression minimization (strengthening option)

To shrink the sealed surface: for matching/scoring the corpus needs the **embedding** (a non-expressive derived vector) + normalized **facts** (ingredient *nouns* without quantities — "feta, phyllo, béchamel"; technique tags — "baked, layered, custard-set"). Facts aren't copyrightable; only expression is. Default the index to "embedding + structured facts," and retain raw verbatim ingredient/step text only for the specific features that genuinely need the literal string (e.g. keyword search on phrasing) — decided feature-by-feature, not "store it all."

## The no-bridge rule (the trap)

The corpus will have a publisher's ingredients sitting in its index; the user, on his device, extracts the same page. The tempting "optimization" is: *don't make him re-extract — just serve the ingredients we already have.* **That single shortcut collapses the entire posture** — the moment the corpus serves source-expressive content down to a user, the corpus is redistributing the copyrighted work.

So: **corpus-internal expressive fields never bridge to the local read path. Re-extraction of the same URL by the local tool is intentional, not waste — the redundancy IS the legal boundary.** Write this as a named anti-pattern wherever the read API is implemented.

## Crawler etiquette (be the search engine you claim to be)

The Corpus's *own* crawl (the batch fetcher / future Playwright path) must behave like a legitimate indexer: identify with a UA, respect `robots.txt`, honor `noindex` / `max-snippet` / `noarchive`, rate-limit. Acting like a search bot *is* part of being one.

Keep this distinct from the **bookmarklet**, which uses the *user's own logged-in session* to capture *his own copy* — a different actor with a different right (Local side). The corpus crawl is the bot; the bookmarklet is the user. Two fetch paths, two products — keeping them distinct is itself part of the separation.

## The read API (the only Corpus → Local channel)

The subscription is read-only and richer than "fetch a recipe":

- `read master recipe` → returns only `corpus_public_view()` output (work product + envelope + link). Never the sealed tier.
- `/match` → Local sends a **vector** (embedding of the user's recipe) → Corpus returns the matched dish + candidates. Numbers up, never content.
- `/grade` → Local sends a **vector + DA/PA numbers** → Corpus returns a percentile/grade by borrowing the matched dish's fit. Numbers up, never content.

Two gates on the read API:
1. **Only vectors/metrics cross upward, never content.**
2. **Reads are stateless** — a `/match` or `/grade` request must NOT persist the subscriber's input. A read endpoint that logs vectors becomes a backdoor contribution path. Subscribers consume; they never feed.

## Local-side posture (for completeness)

The user saw the recipe on the source site and *he* chose to save/process it — treated as his personal-use right, but he can't share it. The full reproducible recipe lives and renders **on his device, by his action**. One hard enforcement on our side: **there is no path that uploads a user's full saved recipe back to any central store.** That keeps "his copy, non-shareable" from quietly becoming "our redistributed copy."
