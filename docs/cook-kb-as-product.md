# Cook KB as a product — design note

**Status:** design only (2026-06-11). Nothing here is built yet. Captures the
direction settled in conversation so Phase 4 and later work build against it.

## The shift

The cook tips/checks knowledge base (`cook_tips_kb`, see `project_cook_kb`) was
built as the **augmentation moat** — curated, owned, in-our-words technique
knowledge the rework's augment pass SELECTs from (never invents; provenance by
`kb_id`, mechanically validated). That investment now pays off twice: the same
curated asset can also become a **visible product surface** — a browsable,
searchable cooking-technique library — and can carry **media** (video/images),
and can host **user-authored private notes**.

The reframe: the KB is not just an invisible feature feeding the cook view; it's
a cooking-knowledge *product*.

## One table → two projections → three surfaces

Keep **one** source-of-truth table. Resist forking ("media tips", "user tips" as
separate tables). The dual/triple purpose is the feature; the differences are
*views*, not *tables*.

- **Two projections** (lenses on the same rows, because the consumers differ):
  - *Augment-selection view* — what `project_published()` already emits:
    technique_tags / trigger_signals / ingredient_classes / equipment / scope /
    mechanism / render_hint. Tuned for the LLM to **match + select**.
  - *Reader view* — title, claim/action/variants, **media**, browse categories.
    Tuned for a human to **read + watch**.
- **Three surfaces:**
  1. **Admin editor** (`forms/cook_kb.html`) — exists. Curate + publish gate.
  2. **Recipe augmentation** (`cook_augment.py`) — exists. Quietly attach to steps.
  3. **Browse / search library** — NEW. The reader destination.

## Media on an entry (video + images)

Additive. The motivating example: a Culinary Institute of America YouTube video on
cutting carrots (`youtube.com/watch?v=y63_TvMVE7E`) attached to a knife-skills
entry, quietly surfaced on recipes with a chop/slice-carrots step.

**The inviolable rule: media is a curated reference that lives on the entry; the
model never emits it.** The augment model still only selects a `kb_id`; code then
renders whatever vetted media that entry carries. A hallucinated URL can never
reach a user — the model picks an entry, the entry's curated media renders. The
anti-invention moat extends to media unchanged.

Proposed shape — a `media` array on the entry:

```
media: [{
  kind: "video" | "image",
  provider: "youtube" | ... ,
  url,
  title,
  source,            # attribution, e.g. "Culinary Institute of America"
  start_seconds?,    # optional deep-link into a video
  note?
}]
```

**Rights stance (consistent with `image_policy` / DL-16): reference + attribute +
embed, NEVER rehost.** YouTube's embed model is creator-sanctioned (the creator
gets the view). This is the *opposite* of the recipe hero-image coopt (where we
localize because we own the use). KB media on third-party content = link/embed/
attribute. Validate provider/URL shape on import.

**"Quietly" is a render contract.** In the cook view, a step whose attached entry
has media shows a subtle affordance (a small ▶ "Knife skills: carrots (CIA)" link
or a collapsed embed) — opt-in, never an autoplay wall. Selection is the existing
tag/trigger match (carrots + a slice/chop step).

## Two axes: `kind` × `source`

User-authored tips introduce an **origin** axis. It is *orthogonal* to `kind` —
do NOT overload `kind` (which stays `tip | check`).

- `kind`   = `tip | check`        — what it is
- `source` = `curated | user`     — whose it is / whether it's the moat
- `owner_user_id` — NULL for curated (house) entries, set for user entries

This mirrors the `recipes` (per-user) vs `master_recipes` (curated corpus) split
exactly — a good sign it's the right shape.

|                | curated (moat)      | user (private)         |
|----------------|---------------------|------------------------|
| **augments**   | any recipe (shared) | owner's recipes only   |
| **browse**     | public library      | owner's own surface    |
| **publishable**| yes                 | never                  |
| **seed export**| yes                 | never                  |
| **media**      | curated refs        | owner's refs           |

### The inviolable guardrail

**User entries never enter the curated projection.** Three filters all gate on
`source='curated'`:

- the **augment-shared projection** (`project_published`) — moat stays house-only
- the **public / SEO library** — we can only publish what we authored
- the **seed export** — the shippable starter pack is curated-only

This is the same **content never flows user → corpus** rule from the split
(`discover_vs_possess` / `project_split_architecture`), enforced in the
projection. Here it pulls triple duty:

1. **IP safety** — a user may paste copyrighted text; it must never surface as
   "our" guidance or get published.
2. **Quality** — user tips bypass the publish gate; they can't dilute the moat.
3. **Publishability** — user notes aren't ours to ship.

One `WHERE source='curated'` away from bulletproof.

### The upside it unlocks

A user's **own** private tips may augment that user's **own** recipes — personal
annotations on the cook view, scoped strictly to
`source='user' AND owner_user_id = me`, rendered with a distinct "your note"
treatment vs the curated provenance badge. Real value (your own trick on your own
braise) without touching the moat. Implementation: a **separate, owner-scoped
injection path** alongside the curated one — never mixed into the cached curated
prefix (the prompt cache stays the curated projection; the per-user slice is a
small uncached add-on).

### Why "not shared" is the smart call

Keeping user tips **private** is what keeps this tractable: no moderation, no IP
review, no abuse surface, no ranking. All deferred by the private decision. If
sharing is ever wanted, the path is the familiar one — a `source='user'` →
curator-review → `source='curated'` **promotion** (same shape as promote-to-
master). Not now.

## The browse / search library

Net-new reader UI, distinct from the admin editor: search by keyword, filter by
technique / ingredient, each tip a card with explanation + embedded media. Two
notes:

- It overlaps the **cookbooks end-user surface** (`project_cookbooks`) and the
  **public read-only pages** roadmap. Unlike recipes (which reference third-party
  sources), the curated KB is **authored in our own words** → cleanly
  publishable, ownable, SEO/AI-crawler-friendly content. A destination we fully
  own.
- Search: the KB is small (~100), so **tag + keyword filtering** is plenty to
  start. Semantic search ("how do I keep things crisp?") is a natural upgrade —
  the sqlite-vec infra is already in place to embed entries (`sqlite_vec_migration`).

## Sequencing

1. **Media schema + provenance-preserving render** — small; unlocks the carrot
   video end-to-end (entry `media[]` → augment selects `kb_id` → cook view renders
   a quiet embed). The first buildable slice.
2. **`source` / `owner_user_id` axis** + the `WHERE source='curated'` guards on
   all three projection/export paths. Then the owner-scoped personal-notes path.
3. **Browse / search library** — its own surface, rides the cookbook / public-
   pages work; keyword+tag first, semantic later.

Phase 4 (the cook-view renderer) should be built knowing #1 is coming, so the
attachment render has a place to put media.

## Open questions (decide at build time)

- Is the public library gated (members) or truly public/SEO? (Curated content is
  publishable, so public is on the table — ties to the public-pages roadmap.)
- `instance`-authored middle tier (portable-package instance owner adds entries
  between house-curated and end-user) — likely a third `source` value later; not
  needed for the curated/user split now.
- Media beyond YouTube (Vimeo, our own hosted clips, diagrams) — provider list is
  extensible; validate per provider on import.
