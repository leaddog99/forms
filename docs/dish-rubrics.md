# Dish Rubrics — Design Note

**Status:** design only, nothing built. Written 2026-08-16.

A **rubric** is a per-dish evaluation framework: the dimensions along which recipes
for that dish actually differ, what the authorities say about each, what our corpus
actually does, and where a given recipe sits. Derived once per dish by a
`dish_rubric` job, stored on the dish, applied to every member recipe.

It exists because of one experiment and one correction to it.

---

## 1. What the experiment showed

Thirty Bolognese recipes from 29 publishers were exported (`scripts/export_markdown.py
--dish "Bolognese"`) and given to a closed-world notebook tool. It produced something
genuinely good: not a summary but an **evaluation framework** — six dimensions on
which bolognese recipes are judged, each with the divergent practice, who does it, and
why one position is better. It then built a master recipe from that framework.

It also made a move worth stealing. **It rejected the majority.** Most of the thirty
use garlic; it said omit garlic, on the authority of Marcella Hazan, Lidia Bastianich
and Vincenzo's Plate. Most pair with spaghetti; it called that an error, citing
Bologna. It treated **frequency as evidence, not as authority** — which is the correct
instinct and the opposite of what a consensus-of-the-corpus approach produces.

### And then the correction

That verdict held **by luck**. It worked because Hazan, Lidia and Vincenzo happened to
be among the thirty. A dish whose corpus is entirely American food blogs would produce
an equally confident rubric that is blog consensus wearing a lab coat.

The Accademia Italiana della Cucina deposited an updated official recipe for *ragù alla
bolognese* with the Bologna Chamber of Commerce on **20 April 2023**, superseding the
1982 text. It contains an explicit **"Unacceptable variants"** list. That is not an
inferred rubric — it is a gauntlet published by the authority:

> **Unacceptable:** veal · smoked pancetta or bacon · only pork · garlic, rosemary,
> parsley or other herbs and spices · brandy instead of wine · flour as a thickener
>
> **Permitted:** mixed beef and pork (~60% beef) · knife-minced meat · cured pancetta
> · a pinch of nutmeg

Against that, the closed-world master recipe:

| Dimension | Accademia (registered) | Closed-world output | |
|---|---|---|---|
| Veal | **Unacceptable** | "can be substituted for half the beef" | contradicts |
| Milk timing | **Halfway through**, after tomatoes | "BEFORE wine and tomatoes" | contradicts |
| Simmer | ~2 hrs (3 by preference) | "minimum 3–4 hrs"; shorter is "the mistake" | overstated |
| Wine | Red **or** white | "dry white" | over-narrow |
| Tomato | 200 g strained + 1 tbsp paste | 800 g whole + 2 tbsp paste | 4× |
| Bay leaf | Herbs unacceptable; nutmeg permitted | "relies on bay leaf and nutmeg" | half wrong |
| Garlic | Unacceptable | Omit | **agrees** |

One right, six wrong, all stated with total confidence.

### The more useful reading

Not "the model was wrong." It synthesised **Hazan**, and Hazan genuinely does
milk-first — a real canonical lineage that disagrees with the registry. There is no
single truth about bolognese; there are **competing authorities**, and a closed-world
read cannot tell you which one it landed on or that others exist.

So a rubric dimension is not always a verdict. Sometimes it is a **contested** field
with named camps, and saying so is more useful and more honest than picking one.

---

## 2. Three kinds of authority, answering three different questions

"Authority" is not one thing, and treating it as one is what makes the Accademia and
Marcella Hazan look like rivals when they are not.

| Kind | Example | Answers | Shape |
|---|---|---|---|
| **Definitional** (de jure) | Accademia, AVPN, DOP/TSG | *What counts as this dish?* | **Thin by design** — a specification, not a teaching text |
| **Instructional** | Hazan, Child, Lewis, Dunlop, Cook's Illustrated, Serious Eats | *How do I make it well?* | Rich; explains why; can disagree with the registry and with each other |
| **De facto** | Traffic, references, what people actually cook | *What do people expect it to taste like?* | Measured, not declared |

The Accademia text being thin is not a weakness — it is a **boundary**, and boundaries
are short. It tells you veal is out and garlic is out. It does not tell you how to get
a silky ragù, and it does not claim to. Hazan does, at length, and is the reference for
Italian cooking in English. They are not competing; they answer different questions.

That resolves the apparent conflict on milk timing. The registry says halfway through;
Hazan says first. **A considered deviation by a recognised authority is categorically
different from a blog deviating** — the first makes a dimension *contested*, the second
makes it a variant. So `dish_canon.weight` cannot be a single scalar: authority is
per-kind, and a dimension's verdict must record which kind it rests on.

Mapping to the rubric:

- **Definitional** authority sets `identity_bearing` — does deviating make it a
  different dish?
- **Instructional** authority supplies `verdict` and `rationale` on technique.
- **De facto** authority is the position distribution — and it must be reported
  **two ways**.

### Count versus reach

A raw count says how many recipes take a position. **Traffic says how many people have
actually eaten that version.** Those differ, and the difference is the finding.

If 3 of 30 bolognese recipes are 40-minute versions but hold most of the traffic, then
the de facto bolognese in an English-speaking kitchen *is* the fast one, whatever the
registry says. A rubric that only counts recipes would report that as a fringe position
and be badly wrong about the world.

**We have the data but not yet the coverage.** `collection_members.traffic` is
populated on 14,641 of 16,179 rows — but those arrive through the **publisher** harvest
(a SEMrush Top Pages file), and dish harvests come through SERP with no traffic figure.
So on the dishes that matter here the coverage is thin: Bolognese 8 of 30, Caesar Salad
5 of 20, Crab Cakes 3 of 30. Traffic-weighted positions are the right design and are
**blocked on backfilling traffic for dish-harvested rows**, not on any new idea.

Until then, report counts and say plainly that they are counts.

---

## 3. Schema

```sql
-- External authorities, with provenance, so any claim is traceable and re-checkable.
CREATE TABLE IF NOT EXISTS dish_canon (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_key      TEXT NOT NULL,
    kind          TEXT NOT NULL,   -- registry | canonical_author | institution | publication
    title         TEXT NOT NULL,   -- "Accademia Italiana della Cucina, updated 2023"
    url           TEXT NOT NULL,
    published_at  TEXT,            -- '2023-04-20'
    extracted_md  TEXT,            -- what we read, kept so a re-derive is reproducible
    weight        REAL DEFAULT 1.0,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dish_rubric (
    dish_key      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    n_recipes     INTEGER NOT NULL,   -- corpus size at derivation
    n_canon       INTEGER NOT NULL,   -- how many external authorities were found
    canon_grade   TEXT NOT NULL,      -- registry | strong | thin | none  (see §6)
    coherence     REAL NOT NULL,      -- modal likelyDish share; gate at 0.80 (see §6)
    mode          TEXT NOT NULL,      -- prescriptive | descriptive
    model         TEXT,
    derived_at    TEXT NOT NULL,
    PRIMARY KEY (dish_key, version)
);

CREATE TABLE IF NOT EXISTS dish_rubric_dimension (
    dish_key         TEXT NOT NULL,
    version          INTEGER NOT NULL,
    key              TEXT NOT NULL,   -- 'garlic', 'milk_timing', 'simmer_time'
    label            TEXT NOT NULL,
    kind             TEXT NOT NULL,   -- categorical | continuous | sequence | pairing
    verdict          TEXT,            -- endorsed position; NULL when contested
    contested        INTEGER DEFAULT 0,
    basis            TEXT NOT NULL,   -- canon | science | corpus_consensus
    identity_bearing INTEGER DEFAULT 0,
    overrules_majority INTEGER DEFAULT 0,
    rationale        TEXT,
    canon_ids        TEXT,            -- JSON array of dish_canon.id
    PRIMARY KEY (dish_key, version, key)
);

-- What the CORPUS does on this dimension. Computed in SQL — never by the model (§5).
CREATE TABLE IF NOT EXISTS dish_rubric_position (
    dish_key      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    dimension_key TEXT NOT NULL,
    position      TEXT NOT NULL,      -- 'uses_garlic' | '0-60min' | 'milk_first'
    n_recipes     INTEGER NOT NULL,
    exemplars     TEXT                -- JSON array of url_normalized
);

-- Per-recipe placement. Keyed on url_normalized, NOT recipe_id.
CREATE TABLE IF NOT EXISTS recipe_rubric_score (
    url_normalized TEXT NOT NULL,
    dish_key       TEXT NOT NULL,
    version        INTEGER NOT NULL,
    dimension_key  TEXT NOT NULL,
    position       TEXT,
    matches_canon  INTEGER,           -- 1 / 0 / NULL when the dimension is contested
    note           TEXT,
    scored_at      TEXT NOT NULL,
    PRIMARY KEY (url_normalized, dish_key, version, dimension_key)
);
```

**`url_normalized`, not `recipe_id`** — the same decision as the activity log, for the
same reason: publisher and dish refreshes are delete-and-replace, and a score keyed on
a regenerated id dies exactly when the row is rebuilt.

---

## 4. The job

`dish_rubric`, run per dish, following the existing CLI convention — identity on the
command line, never a query:

    python -m jobs run dish_rubric --dish "Bolognese"

Phases:

1. **Research canon.** Web search + fetch for authoritative texts. Store in
   `dish_canon` with the extracted markdown, so a later re-derive does not depend on
   the page still existing. *The Accademia PDF was read through our own
   `to_markdown/pdf_to_markdown.py` — the intake pipeline works on canon as well as on
   recipes.*
2. **Compute corpus statistics** in SQL. Presence/absence per ingredient concept, time
   buckets, step counts, equipment, pairing.
3. **Derive dimensions** — one model call over canon + statistics + recipes.
4. **Score every member recipe** against the stored rubric.

Cost is roughly one research pass and one derive call per dish, plus a cheap
per-recipe placement. Versioned, so a re-derive never overwrites history.

---

## 5. The model judges; code counts

This is the hard-won rule, and it comes from verifying the same experiment.

Every **judgment** the notebook made was sound — including flagging a published recipe
that calls for 1 tablespoon of baking powder in a 1-pound crab cake batch, which we
confirmed exactly. Every **count** it made was wrong:

| Claim | Actual |
|---|---|
| egg "used by 20 of the recipes" | 30 of 30 |
| "exactly half ban vegetables" | 17 of 30 |
| saltine camp — 6 publishers named | 10 contain saltines |

So `dish_rubric_position` is populated by SQL and handed to the model as fact. The
model never counts. It interprets counts, names dimensions, and writes rationale. This
is the same boundary the cook-view work already draws: the model emits judgment, code
does the arithmetic.

---

## 6. Two questions, not one

Whether a rubric is possible turns on two independent things, and conflating them was
a gap in the first draft of this note.

| | **Canon exists** | **No canon** |
|---|---|---|
| **Dish-shaped** | Full rubric with verdicts | **Descriptive rubric** — dimensions and what each choice *does*, no "should" |
| **Not dish-shaped** | irrelevant | No rubric. It is a collection, not a dish |

### Is it dish-shaped? — measurable today

`_identity.likelyDish` is stamped on all 5,169 master rows, so **coherence** is one
query: the share of a dish's members whose `likelyDish` agrees with the modal value.

    Cacciatore, Caesar Salad, Koshari .......... 100%   (1 distinct value)
    Banana Cream Pie, Gricia, Red Beans & Rice ..95%
    Pasta alla Norma, alla Nerano .............. 90%
    ---------------------------------------------------
    Arrabbiata, Cowboy Beans ................... 25%
    Zucchini ................................... 24%   (13 distinct)
    Ramen ...................................... 20%
    Mussels & Moules, Swordfish, Blood Orange .. 10-15% (18-19 distinct)

The low end is not a data problem — those entries are **ingredients or categories**
wearing a dish's clothes. "Blood Orange" spans 10 chapters and 18 distinct dishes; a
rubric for it would be nonsense. "Mussels & Moules" is a category containing *moules
marinière*, Thai mussels and a dozen others, each of which might deserve its own rubric.

**Proposed gate: coherence ≥ 0.80 to derive a rubric.** Below ~0.40 the entry should be
treated as a browse collection instead, and possibly split into real dishes. This also
gives the dish taxonomy a health metric it does not currently have.

### Measured, all 164 dishes (2026-08-16)

| Band | Count | Meaning |
|---|---|---|
| **DISH** ≥ 0.80 | **66** | rubric viable today |
| **MIXED** 0.40–0.79 | **61** | review; several want splitting |
| **COLLECTION** < 0.40 | **37** | not a dish |

Full table at `temp/dish_coherence.tsv`. The bottom of the list sorts into clean types
— **ingredients** (Blood Orange 0.10, Swordfish 0.15, Lingonberry 0.12, Orzo 0.12),
**pasta shapes** (Tagliatelle 0.10), **condiments** (Hoisin Sauce 0.10, Dijon 0.22),
**cuisines** (Cajun 0.18) and **categories** (Ramen 0.20, Mussels & Moules 0.13). None
of those can carry a rubric, and saying so is useful in itself.

**One caveat before acting on the number: low coherence has two causes.** Most of the
bottom really is a collection. But *Kolokithopita* (0.10) and *Gemista* (0.17) are
single real dishes whose members disagree about the NAME — "zucchini pie" versus
"kolokithopita", "gemista" versus "yemista". That is transliteration variance in
`likelyDish`, not a taxonomy fault, and demoting them would be wrong. The gate should
therefore flag for review rather than auto-exclude, and a name-clustering pass (the
embeddings already exist) would separate the two causes.

### Is there canon? — the ladder, not a binary

Bolognese has a registry, which made it a flattering first example. Most dishes do not,
but "registry or nothing" is a false choice. In descending order of authority:

1. **Registry / protected designation** — Accademia, AVPN, DOP/TSG. Rare and decisive.
2. **Origin document** — the first published version. Toll House for chocolate chip
   cookies. Authority by primacy rather than by decree.
3. **Canonical author** — Hazan for Italian, Child for French, Lewis for Southern,
   Dunlop for Sichuan. The recognised reference for a cuisine.
4. **Methodological authority** — Cook's Illustrated, Serious Eats. Not tradition:
   they have *tested the variables*. For a dish with no tradition this is often the
   best available source, and it is the right kind of authority for "does resting the
   dough matter".
5. **Food science** — universal and falsifiable, needing no canon at all. Creaming
   versus melted butter changes crumb; collagen becomes gelatin over hours. This
   carries dishes with zero tradition.
6. **Nothing** — descriptive only.

### When canon runs out, the rubric turns predictive

This is the important move. Without an authority the rubric stops saying **"you
should"** and starts saying **"this produces"**:

> Banana bread splits 60/40 on butter versus oil. Butter gives a finer crumb and more
> flavour; oil gives a moister loaf that keeps longer. Neither is wrong.

That is more useful to a cook than a verdict would have been, and it cannot be wrong in
the way a false verdict is wrong. It also means **levels 4–6 of the ladder carry most
dishes** — food science and tested methodology answer "what does this choice do" without
needing anyone to have decreed anything.

And the absence of canon is itself worth stating. *"No authority exists and the field
does not agree"* tells a cook this is a free-form dish where preference rules — a real
finding, not a gap to paper over.

### The long game: our own outcome data

For canon-less dishes there is an authority nobody else can assemble — **which versions
people actually cook, and cook twice**. A registry records tradition; engagement records
outcome. That is years away and needs real users, but it is the eventual answer to "who
says so" for the dishes tradition never ruled on, and it is unique to us. See the
activity-and-engagement design.

---

## 6a. Recording what was found

Bolognese has a registry. Carbonara, amatriciana and pizza napoletana have comparable
texts. **Banana bread does not.** The job must record what it found and degrade
honestly rather than promoting blog consensus to authority — which is precisely the
failure this design exists to prevent.

| `canon_grade` | Meaning | Rubric behaviour |
|---|---|---|
| `registry` | A deposited or protected specification | Verdicts cite it directly |
| `strong` | Multiple canonical authors / institutions | Verdicts cite; contested where they differ |
| `thin` | One or two respected sources | Verdicts allowed, marked low-confidence |
| `none` | Nothing authoritative found | **No verdicts.** Positions and distributions only |

At `none` the rubric still has value — it can say "3 of 30 do this" — but it must not
say "should".

**We cannot currently assess corpus provenance.** Of the 30 Bolognese publishers,
`domains.country` is unknown for 28 and 'United States' for 2. So the risk of an
all-American-blog corpus producing confident Italian verdicts is real and unmeasurable
today. Populating `country` on the domains master is a prerequisite for trusting
`corpus_consensus` as a basis.

---

## 7. Fidelity is not quality

The most important product decision here, and the easiest to get wrong.

A 40-minute bolognese is **not bad**. It is not canonical. If the rubric renders as a
grade, every weeknight recipe fails and we become the gatekeeper nobody asked for —
against a product whose whole promise is helping people cook well.

Two separate readouts, never blended into one number:

- **Fidelity** — how close to canon, per dimension, with citations.
- **Quality** — how good of its kind, which is a different question and mostly the
  existing ranking's job.

This mirrors the two-stage selection rule already settled elsewhere: harvest selects,
OU ranks within the pool. Here canon describes, and the rubric never overrides the
cook's intent.

---

## 8. What it is for

1. **Recipe critique.** A "how this compares" panel: *"40-minute simmer — fastest 3 of
   30. Includes garlic, which the registered recipe excludes. Served on spaghetti;
   Bologna pairs ragù with tagliatelle."* Derived, cited, and checkable.
2. **A second ranking axis.** OU is SEO authority. The rubric is culinary. Keep them
   apart; a cook cares about the second.
3. **Editorial that requires the whole field.** The master recipe the experiment
   produced — publishable, and impossible for any single publisher to write.
4. **Corpus QA.** The baking-powder catch generalises: an outlier on a dimension with
   no compensating reason is a flag on our own data.

**41 dishes already have ≥20 member recipes; 102 have ≥10** (164 stamped in total), so
this is viable across a real share of the corpus now.

---

## 9. Naming

**Do not call this the gauntlet.** That term already means the accept/reject pipeline
in the corpus-ML work, and reusing it will confuse both. It is a *rubric*; its
per-recipe output is a *report card*.

---

## 10. Open questions

1. **Quantitative dimensions are blocked.** Ratios — meat to soffritto, crab to filler,
   tomato load — need parsed amounts, and `recipeIngredient` is 59,366 plain strings
   with zero parsed. **Categorical dimensions work today** (garlic yes/no, milk timing,
   pasta shape, herb presence), so ship those first and add ratios after the ingredient
   parser.
2. **How is a contested dimension rendered?** Accademia and Hazan genuinely disagree on
   milk timing. Showing both camps is honest; showing neither as correct may read as
   evasion. Probably: name the camps, and let the user's own history pick a default.
3. **Refresh cadence.** Canon changes rarely — but it *does*: the 2023 update exists
   because hanger steak became hard to buy. Annual re-research, or on demand.
4. **Does the rubric feed the taste profile?** Rubric positions are far more legible
   than an embedding centroid — "you pick the fast end, you skip dairy" is a profile a
   user could read and correct. That is a strong fit with the visible-profile principle
   in the activity/engagement design.
5. **Equipment normalisation limits pairing dimensions** — 3,004 distinct equipment
   names across 33,729 mentions, 261 ways to write "skillet".

---

## 11. Build order

0. **Compute coherence for all 164 dishes.** One query, no model, no cost. It tells us
   which dishes can carry a rubric at all, and doubles as a health metric on the dish
   taxonomy — "Blood Orange" at 10% is a browse collection filed as a dish.
1. **`dish_canon` + the research phase, for Bolognese only.** The Accademia text is
   already extracted at `temp/accademia_ragu.md`. Prove the fetch-and-store loop.
2. **Corpus statistics in SQL** for a handful of categorical dimensions on the same
   dish. No model involved.
3. **One derive call**, storing dimensions with `basis` and `contested` populated.
4. **Score the 30 members** and render the comparison panel on one recipe page.

If that panel is genuinely useful on a recipe you already know well, the rest follows.
If it is not, we have spent a day rather than a quarter.
