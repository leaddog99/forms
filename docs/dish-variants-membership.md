# Dish Variants & Recipe Membership — Design Note

Status: **design, not built.** Worked out in discussion on 2026-06-03.
No code has changed.

This note went through a full many-to-many detour before landing on a
**one-to-one** model. The detour is preserved in §1 because the reasons it
was rejected are the load-bearing part — they're what justify the simple
answer.

Audience: anyone about to touch `dishes`, `master_recipes`, the batch
delete-and-replace, the grader, or `find_best_dish_match`.

---

## 1. The problem, and how we landed here

We wanted to run a dish batch for a *slice* of a dish — e.g. "Spanakopita,
but only Greek-sourced recipes" — via a locale-qualified search pattern.
That raised a chain of questions:

1. **Can the embedding matcher tell a Greek spanakopita from a US one?**
   No. Both sides embed via `compose_identity_text` (`embeddings.py:82`),
   built from *identity* (`likelyDish`, `cuisine`, `technique`), not
   *provenance*. A spanakopita is Greek no matter who published it, so the
   vectors are near-identical. The splitting axis isn't in the vector.

2. **So make the slice a second dish row?** A `dishes.name` is the PK and
   `master_recipes` enforces `UNIQUE(url_normalized, user_id)`
   (`save_recipe_api.py:725`) → one URL → one master row → one
   `_master.dish` stamp. Two overlapping spanakopita dishes **steal** shared
   URLs from each other on refresh (last-writer-wins via the
   `ON CONFLICT(recipe_id) DO UPDATE` upsert, `save_recipe_api.py:3668`).

3. **Route a recipe to the right slice by source language?** Too narrow —
   a variant can be anything (diet, pastry, technique, region), not just
   language. No general auto-routing rule exists.

4. **Then a recipe can be in several variants at once (vegan AND Greek), so
   go many-to-many with a junction table?** This is the detour. It works,
   but it collides with the *original purpose*: cohort matching exists to
   **grade a new recipe** — one "how exceptional is this?" number.
   Many-to-many produces **one grade per cohort**, i.e. several grades for
   one recipe. That's the wrong shape for the thing we actually wanted.

5. **Final decision: one-to-one.** A recipe belongs to **exactly one**
   variant. When more than one variant could claim it, that's a **tie the
   curator resolves**, not a stored double-membership. One membership → one
   cohort → one grade. The "steal" from §2 stops being a silent bug and
   becomes the explicit tie-resolution event.

The genuine multi-membership case (a recipe legitimately curated into two
*unrelated* collections — "Greek Classics" *and* "Spanakopita") is the only
thing one-to-one can't express. We are deciding we don't need it; if that
changes, §7 notes where the junction would come back.

---

## 2. The model

```
master_recipes      one row per URL — recipe content + a single _master.dish stamp   (≈ today)
dishes              one row per VARIANT — name + queries + cohort config + OU fit     (today's "dish")
dishes.dish_group   NEW column — the header that groups variants ("Spanakopita")
dish_conflicts      NEW small table — pending ties for a curator to resolve
```

- A **variant** is just today's `dishes` row: unique `name`, its own
  `queries` (the search pattern — locale/diet/technique-qualified, whatever),
  its own cohort, its own refresh schedule.
- A **header** (`dish_group`, nullable) groups variants for browse/display.
  `NULL` → a standalone dish (today's behavior, unchanged). Purely
  organizational.
- **Membership is one-to-one.** A recipe carries a single `_master.dish`
  pointing at the one variant it's filed under — exactly the stamp that
  exists today. No junction.
- **Ties** (a second variant wants a recipe another variant already owns)
  are detected and parked in `dish_conflicts` for a curator to resolve —
  they are **not** silently overwritten (§3).

```
dish_group: "Spanakopita"
   ├── dishes.name "Spanakopita · Greek"   queries=[…el…]
   └── dishes.name "Spanakopita · Vegan"   queries=[vegan…]

   master_recipes(url=…/vegan-greek-spanakopita)  _master.dish = "Spanakopita · Greek"   ← ONE home
        (the Vegan batch also surfaced it → a dish_conflicts row → curator confirms or moves it)
```

---

## 3. Tie resolution — the one new behavior

This is the only genuinely new machinery, and it's what makes one-to-one
safe (it replaces the silent last-writer-wins steal).

**Detection (at batch save).** When variant *B*'s batch would save a URL
that already has a `master_recipes` row stamped `_master.dish = A` with
`A ≠ B`:
- **Do not overwrite.** Leave the recipe filed under *A*.
- Insert a `dish_conflicts` row: `(url, current_dish=A, claimant_dish=B,
  run_started_at, status='pending')`.
- Surface pending conflicts in the dish/curator UI.

**Resolution (curator).** The curator picks the winner → sets
`_master.dish` to the chosen variant (and re-grades, §5). If the same
variant re-surfaces its own URL on a later refresh, that's **not** a
conflict — just a re-rank.

Volume is low: locale/diet-qualified query sets make variants mostly
disjoint, so genuine cross-variant collisions are the exception, not the
rule. The conflict queue is a light curator task, not a firehose.

Same mechanism covers cross-*header* collisions (Spanakopita vs Tiropita
both claim a URL) — the curator picks the right dish or marks "neither."

---

## 4. Schema

```sql
-- Header column on the existing dishes table. NULL = standalone dish.
ALTER TABLE dishes ADD COLUMN dish_group TEXT;        -- via ensure_* guard, idempotent

-- Pending ties for a curator. Low volume.
CREATE TABLE IF NOT EXISTS dish_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_normalized  TEXT NOT NULL,
    current_dish    TEXT NOT NULL COLLATE NOCASE,   -- where the recipe is filed now
    claimant_dish   TEXT NOT NULL COLLATE NOCASE,   -- the variant that also wants it
    run_started_at  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'  -- pending | resolved
                    CHECK (status IN ('pending','resolved')),
    resolved_to     TEXT,                            -- the winning dish, once decided
    created_at      TEXT NOT NULL,
    UNIQUE(url_normalized, claimant_dish)
);
CREATE INDEX IF NOT EXISTS idx_dish_conflicts_status ON dish_conflicts(status);
```

`master_recipes` is **unchanged** — one row per URL, `UNIQUE(url_normalized,
user_id)`, single `_master.dish` stamp. `_master.exceptionalism` /
`_master.kind` stay where they are. No junction, no migration of existing
stamps — today's rows are already one-to-one.

---

## 5. Grading — one grade, against the recipe's own variant cohort

Cohort grading (the original purpose) is **singular** (one membership → one
grade) and is computed against the **variant the recipe is filed under** —
*not* the pooled header.

We considered pooling all variants of a dish and grading against the whole
pool, and **rejected it.** OU is a *residual* against a PA-on-DA fit. A pool
dominated by high-authority US sites encodes the US authority landscape;
Greek recipes live on lower-DA Greek-language sites with a different PA-on-DA
relationship, so they'd sit below a curve that isn't theirs → systematically
negative residuals → unfairly low grades. A spanakopita that is excellent
*among Greek recipes* would be graded mediocre for the crime of being Greek —
which defeats the entire reason for a Greek variant. So:

**Grade each recipe against its own variant's cohort.** Mechanically this is
*today's* path — a variant is a `dishes` row, and a `dishes` row already
carries its own `last_ou_fit` (`dishes.py:84`). No new grading machinery, no
"recompute the whole group on every update": refreshing the Greek variant
recomputes only the Greek fit, exactly as a dish refresh does now. The header
is therefore **purely organizational** — it holds no pooled fit.

Thin-cohort fallback ladder (a lone variant is often below the OU-fit floor,
`_MIN_FIT_N = 25`, `chapters.py:42`):

```
variant cohort (n ≥ 25)  →  fit + grade against its own kind     ← fair, default
   else                  →  chapter-level fit (chapters.py)      ← broader backstop, FLAGGED in UI
```

Fall back to the **chapter** fit, *not* the header pool — the chapter fit is
the system's existing "cohort too thin" backstop and isn't biased toward one
variant's authority landscape. Surface the fallback ("graded against all
[chapter] until the Greek cohort grows").

A standalone dish (no variants) *is* its own cohort → graded against itself →
exactly today's behavior, unchanged.

---

## 6. Assignment & the matcher

### Today: the matcher only *grades*, it does not *file*
`find_best_dish_match` (`embeddings.py:377`) embeds the recipe, KNN-searches
`dishes_vec`, and returns the nearest dish above threshold. Its result is
consumed **only** by `compute_exceptionalism` (`save_recipe_api.py:3336`) and
recorded *inside the grade block* as `…exceptionalism.basis.matched_dish` /
`_grade.basis.matched_dish`. **It never writes `_master.dish`.** So a recipe
carries two distinct dish notions:

| field | who sets it | meaning |
|---|---|---|
| `_master.dish` | the **batch** (lineage) | curation membership — "this dish's batch produced me" |
| `…basis.matched_dish` | the **matcher** | the cohort used to grade me — recorded, not filed |

### The change: promote the match to an assignment
The model in this note treats the matcher's nearest dish as an **assignment**:
on a non-lineage recipe, write the matched dish into `_master.dish` /
`_grade.dish` (not just the grade basis), and **recompute on edit** — when the
record changes, re-match and potentially **reassign** (grading already
recomputes per save, so this is the same trigger). Lineage (batch) assignment
still wins when present; the matcher fills the gap for everything else.

### Exact match logic (so the assignment is well-defined)
1. `compose_recipe_text` → embed text from the `_identity` card
   (`likelyDish · cuisine · ethnicity · primaryIngredients · technique`).
2. Embed (`text-embedding-3-small`, L2-normalized).
3. KNN over `dishes_vec`, **Tier 1** pre-filtered to the recipe's
   `classification.chapter` (`vector_store.py:230`); **Tier 2** wide scan if
   no in-chapter dish or top result below threshold.
4. L2 → cosine (`1 − dist²/2`); below `DEFAULT_MATCH_THRESHOLD` → no match.
5. Nearest qualifying dish is the assignment + the grading cohort.

---

## 7. Language as a match dimension — and an editable field

Variants are **identity-degenerate**: a Greek and a US spanakopita produce
the same identity card → the same vector. The embedding therefore *cannot*
separate them, and adding a "Greek" token to the embed text won't reliably
either. Language must enter the match as a **hard pre-filter**, the same rail
`chapter` already rides:

```sql
WHERE v.embedding MATCH ? AND k = ?
  AND v.name IN (SELECT name FROM dishes
                 WHERE chapter = ?
                   AND source_language = ?)   -- NEW dimension, same pattern as chapter
```

- The recipe supplies the filter value from `_source.originalLanguage`
  (already captured by `intake/translate.py` per
  [[project_multilingual_extraction]]).
- Each dish/variant carries `source_language` (the locale it harvests).
- Within the filtered candidate set, cosine picks the nearest — so a
  Greek-language recipe only *sees* Greek variants and is assigned correctly.
  **The vector handles identity; the filter handles provenance.**

### `originalLanguage` becomes an editable recipe field (not a read-only pill)
Because language now drives assignment + grading, it is load-bearing and
**must be curator-correctable** — `<html lang>`/heuristic detection misfires
(English-templated Greek sites, multilingual pages), and a wrong `el`/`en`
would silently re-route and re-grade the recipe.

- **Control:** a language `<select>` (ISO 639-1) in the recipe form, defaulting
  to the detected value; `translated` as a checkbox; `originalTitle` shown
  when set. The compact pill is just the collapsed view of this control.
- **Already modeled:** `_source.originalLanguage` exists
  (`recipe_model.py:254`) — this is additive UI + round-trip, not a schema
  change ([[feedback_recipe_model_first]]). Audit all four edges —
  load / save / extract / metadata — so it isn't dropped on save
  ([[feedback_db_form_sync]]).
- **Recompute on edit:** changing the language re-runs the §6 match → may
  reassign the dish/variant → regrades. The `<select>` is one of the inputs
  that triggers re-match on save.
- **Override persistence:** detection fills `originalLanguage` only when
  blank; a curator-set value is authoritative and a later re-extract must
  **not** clobber it (same "human override wins" rule as `native_name`).

Display status today: **nowhere.** `originalLanguage`/`translated`/
`originalTitle` are modeled and populated at extract time but rendered in no
UI (grep of `recipe_form_styled.html` finds none) — this section is what
gives them a surface.

---

## 8. Touch-points

1. **batch save / delete-and-replace** (`dishes.delete_master_rows_for_dish`,
   `dishes.py:680`; the upsert at `save_recipe_api.py:3668`) → add the §3
   conflict check: if a URL is already filed under a different variant, park a
   `dish_conflicts` row instead of overwriting `_master.dish`.
2. **cohort reads** (top-N, counts, browse) → unchanged shape
   (`json_extract(_master.dish)`), but **group by `dish_group`** for the
   header view.
3. **grading** (`compute_exceptionalism`, `save_recipe_api.py:3336`) →
   per-variant fit (§5): grade against the recipe's own variant `last_ou_fit`,
   chapter fallback when thin.
4. **the matcher** (`find_best_dish_match`, `embeddings.py:377`) → add the
   `source_language` pre-filter (§7); promote the result to a persisted
   **assignment**, not just a grade-basis note (§6); recompute on edit.
5. **the recipe form** → editable language `<select>` + `translated` +
   view-original (§7), wired into the four-edge round-trip and the re-match
   trigger.
6. **curator UI** → a pending-conflicts list with one-click winner pick; the
   dish editor groups variants under their `dish_group`.

---

## 9. On the shelf (not this change)

- **Header-level identity.** Since variants are identity-degenerate, the
  identity card + embedding + `native_name`/`native_language` should live
  **once per header**, not be repeated per variant (per-variant embeddings
  would just make the matcher pick one arbitrarily). For standalone dishes
  the dish itself carries them (today). See [[project_identity_card]].
- **`aka` / alternateNames on the card.** The disambiguation signal the
  retrieval `queries` carry ("Greek lasagna" → Pastitsio) belongs in the
  card as a symmetric, denoised `aka` field — not by re-injecting SEO-noisy
  one-sided query strings into the embedding. Note `description` was added
  for this but isn't in the carded embed path (`compose_identity_text` reads
  card fields only), so that signal is currently dormant on carded dishes.
- **native_name vs source-locale are different axes** — origin (header
  identity) vs which sites a variant harvests (variant config). They
  coincide for Spanakopita·Greek; they wouldn't for a US-sourced Tacos
  variant (Mexican origin, English sources).
- **The junction would return** only if we need a recipe in two *unrelated*
  cross-cutting collections at once. One-to-one + curator tie-break is the
  decision until a real need appears.

---

## 10. Relationship to the dish-catalog seed

The seed of ~1,563 dishes from `chapter_shortcuts.json` is **unaffected**:
each seeded dish is created with `dish_group = NULL` (standalone), single
membership, today's grading. Variants and the conflict queue appear only
where a curator deliberately creates a split. So this lands independently of,
and after, the seed.
