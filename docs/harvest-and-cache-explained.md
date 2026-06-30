# How a Recipe Travels From SEMrush Into Our Corpus (and How the Cache Keeps It Cheap)

*Written to be understandable by a sharp high‑school junior. No prior knowledge of the
code needed. Real file/function names are tucked into "Under the hood" notes so it also
works as a reference.*

---

## 1. The 30‑second version

We collect great recipes from cooking websites ("publishers"). For each publisher we:

1. Get a **list of their most‑popular recipe pages** (from a tool called SEMrush).
2. **Visit each page**, check "is this actually a recipe?", and **score** it.
3. Keep the **best ones** and **read the recipe off the page** into our database.

Step 3 is the expensive part, because "reading the recipe off the page" uses an **AI
model** that costs money and time. So we **remember** what we already read (a *cache*),
and we only re‑do the expensive work when the recipe on the page has **actually changed**.

Think of it like a smart student doing a big research project:
- They don't re‑read a book every time they need a fact — they keep **notes** (the cache).
- But if a book comes out in a **new edition with changed content**, they re‑read *that
  one* (change detection).
- They don't re‑read a book just because it got a **new cover or a new printing date**
  (we ignore dates/ratings — more on that later).

---

## 2. The cast of characters

| Thing | Plain meaning | Under the hood |
|---|---|---|
| **Publisher / domain** | A cooking website, e.g. `sallysbakingaddiction.com` | `domains` table |
| **SEMrush export** | A spreadsheet of a site's top pages by traffic | `*-organic.PagesV3-*.xlsx` |
| **Harvest** | The whole job that turns that list into saved recipes | `publisher_refresh` job |
| **The recipe cache** | Our saved copy of each *finished* recipe | `llm_extract_cache` table |
| **The page cache** | Our saved copy of each *raw web page* | `page_cache.db` |
| **The fingerprint** | A short code that changes only when the recipe content changes | `compute_recipe_fingerprint` |
| **The corpus** | Our real library of recipes | `master_recipes` table |

Two different caches matter, and people mix them up, so to be crystal clear:

- **Page cache** = the *raw ingredients of the web page* (the HTML we downloaded).
  Saving this means we don't have to **re‑download** the page.
- **Recipe cache** = the *finished dish* (the cleaned‑up recipe the AI produced).
  Saving this means we don't have to **re‑run the AI**.

The AI step is the costly one, so the recipe cache is the big money‑saver. The page cache
is a smaller bonus saver (network/credits).

---

## 3. The journey of one recipe URL (the happy path)

Let's follow a single page, `sallysbakingaddiction.com/best-banana-bread-recipe`, through
the whole harvest.

### Step 1 — Get the list (SEMrush)
A human opens SEMrush, exports the publisher's "Top Pages" report (a spreadsheet of the
most‑visited URLs), and saves the `.xlsx` file. The harvest reads that file to get the
candidate URLs, already ranked by how much traffic each gets.

> **Why a human, not a robot?** SEMrush's terms don't love automated scraping, and the
> export is free and quick by hand. So the *system* schedules and bookkeeps; the *human*
> just clicks "Export → Save." 
> **Under the hood:** `collections_lib.backlinks_file_path` finds the file;
> `harvest_publisher_top` reads it.

### Step 2 — Visit each page and ask "is this a recipe?"
For every candidate URL, we **download the page** and check whether it's really a recipe
(some links are category pages, "best of" listicles, restaurant reviews, etc.). We look
for structured recipe data (called **JSON‑LD** — a hidden block of data many recipe sites
include) or, failing that, for the tell‑tale shape of a recipe (an ingredients section
**and** a method section).

> **Under the hood:** the "is‑recipe filter," `_is_recipe_filter`. The download goes
> through one shared function, `fetch_with_full_fallback`.

### Step 3 — The page cache saves the second download
Here's a subtle but important trick. The harvest looks at each page **twice**: once to
check "is it a recipe?" and later to actually read the recipe. Without help, that's **two
downloads** of the same page.

So the first download is **saved in the page cache** (`page_cache.db`), and the second
look just **reuses it**. One download, not two. On sites we have to pay to access (via an
"unblocker"), that literally **halves the cost per page**.

> **Under the hood:** `fetch_with_full_fallback` checks/writes `page_cache.py` when the
> harvest turns it on (a per‑run switch). It only saves *successful, real* pages (never an
> error page or a "you're a robot" block screen).

### Step 4 — Score and keep the best
The recipes that pass the filter get a **quality/authority score** (using a service called
Moz, plus traffic numbers), get ranked, and the **top N** are marked as "winners." The
rest are remembered but not pulled in.

### Step 5 — Read the winners into the corpus (the expensive part — but usually skipped!)
For each winner, we want the finished recipe in our library. Before doing any expensive
work, we ask three questions, in order:

1. **Do we already have this recipe saved?** (Is it in the recipe cache?)
2. **Is our saved copy still fresh?** (Less than 30 days old, same AI model, same
   extraction instructions?)
3. **Has the recipe on the page actually changed since we saved it?**

If we have it, it's fresh, **and** the page hasn't changed → we **reuse our saved copy.
No AI. No cost.** This is the normal case on a re‑harvest, and it's the whole point.

If anything fails those checks → we **re‑read the recipe** and **update our saved copy**.

> **Under the hood:** `extract_recipe_from_url(revalidate=True)`. "Revalidate" is the mode
> that does the change‑check before reusing.

That's the happy path. The next sections explain the clever bits: **how we tell if the
page changed**, and **all the exceptions**.

---

## 4. The fingerprint: how we tell if a recipe changed (without re‑reading it)

We need a cheap way to answer "did this recipe actually change?" Re‑running the AI to find
out would defeat the purpose. So we use a **fingerprint**.

A fingerprint is a short code (a *hash*) made by mashing together **only the parts that
matter**:

- the recipe's **name**,
- its **ingredients**, and
- its **instruction steps**.

Then we lowercase and squish them and run them through a one‑way math function. Same
content → same code. Change an ingredient → totally different code.

**What the fingerprint deliberately IGNORES** (this is the important part):

- the **"last modified" date** on the page,
- the **description / blurb**,
- the **star rating and review count**,
- the **images**.

Why ignore those? Because publishers change them **all the time without touching the
actual recipe** — they re‑publish the page, the date bumps, a new review comes in, the
rating ticks from 4.7 to 4.8. If we keyed off the *date*, we'd re‑read the recipe for
nothing, over and over. By fingerprinting **only the recipe content**, a date or rating
bump leaves the fingerprint **unchanged**, and we correctly reuse our copy.

> This was a real design question: *"Could a date bump accidentally change the
> fingerprint?"* The answer is no, by construction — dates aren't part of the fingerprint.
> **Under the hood:** `compute_recipe_fingerprint` (in `extract_cache.py`).

### One more subtlety: we fingerprint the *source*, not our finished copy
For recipes we translate (e.g. Greek sites → English), our **saved copy is in English**
but the **page is in Greek**. If we compared "English copy" vs "Greek page," they'd never
match and we'd re‑translate every time.

So we fingerprint the **raw page recipe (before translation)** and store that. On the next
harvest we fingerprint the raw page again and compare **Greek‑to‑Greek**. Same source →
match → reuse the English copy. This is why it works for foreign‑language sites too.

> **Under the hood:** the source fingerprint is
> `compute_recipe_fingerprint(jsonld_to_recipe(raw page JSON‑LD))`, stored in the
> `source_fingerprint` column.

---

## 5. What "re‑read the recipe" actually means (two roads)

When we *do* need to read a recipe (cache miss, or the page changed), there are two ways,
and we always prefer the cheaper one:

1. **JSON‑LD‑direct (free):** If the page includes that hidden structured data block, we
   convert it straight into a recipe — no AI needed. Most big recipe sites have this.
2. **Markdown → AI (costs money):** If there's no usable structured data, we turn the page
   into clean text and ask the **AI model** to pull out the recipe. This is the expensive
   path.

So even when a recipe *changes*, if the site has good structured data, the re‑read is
**still free**. The paid AI only fires for pages without structured data (or when the
structured data is broken).

After reading, we also do a quick **enrichment** pass: figure out the cookbook chapter,
grab a **screenshot** of the page, and build a small "identity card" of the dish.

> **Under the hood:** `jsonld_to_recipe` vs `markdown_to_recipe`; the enrichment tail in
> `extract_recipe_from_url`.

---

## 6. Updating the cache (the "refresh")

When we read a recipe (fresh or changed), we **write it back** to the recipe cache:

- the finished **recipe**,
- its **fingerprint** (so next time we can compare),
- a fresh **timestamp** (which restarts the 30‑day clock).

When we **reuse** a recipe (nothing changed), we **don't** rewrite it — that would waste
work and reset the clock for no reason.

**A special case — older saved recipes:** some recipes were cached *before* we invented the
fingerprint, so they have a blank one. The first time we reuse such a recipe, we quietly
**fill in just the fingerprint** (without rewriting the recipe or resetting the clock), so
the *next* harvest can do proper change‑detection on it.

> **Under the hood:** `set_cached_extract` (full write) vs `backfill_source_fingerprint`
> (fills only the blank fingerprint).

---

## 7. The full picture, as a diagram

```
SEMrush export (.xlsx)  ──►  list of candidate URLs (by traffic)
        │
        ▼   for each URL
   download page  ──►  saved in PAGE CACHE  (so we download once, not twice)
        │
        ▼
   "is this a recipe?"  ──►  no  → drop it
        │ yes
        ▼
   score + rank  ──►  keep top N as "winners"
        │
        ▼   for each winner
   ┌─────────────────────────────────────────────────────────────┐
   │  Do we have it cached, fresh, AND unchanged?                 │
   │     ├─ YES → REUSE saved recipe        (no AI, no cost) ✅   │
   │     └─ NO  → READ recipe:                                     │
   │               ├─ page has structured data → JSON‑LD (free)   │
   │               └─ otherwise → AI model (costs money)          │
   │             then WRITE it to the RECIPE CACHE (+ fingerprint)│
   └─────────────────────────────────────────────────────────────┘
        │
        ▼
   save into the corpus (master_recipes)
```

---

## 8. The exceptions (where it gets interesting)

Real life is messier than the happy path. Here's every important "what if," in plain terms.

| Situation | What happens | Why |
|---|---|---|
| **Re‑harvest, nothing changed** | Reuse the saved recipe. **No AI, no re‑download.** | The fingerprints match and the copy is fresh. This is the normal, cheap case. |
| **The recipe genuinely changed** | Re‑read *that one* recipe and refresh its cache record (new recipe, new fingerprint, clock reset). | Fingerprints differ → the source changed. |
| **Only the date / rating / description changed** | **Reuse** — treated as unchanged. | The fingerprint ignores those fields on purpose, so cosmetic bumps don't cause needless re‑reads. |
| **Page has no structured data (no JSON‑LD)** | We can't compute a cheap fingerprint, so we **fall back to the 30‑day rule** (reuse for 30 days, then re‑read). | Without structured data, the only cheap "is it different?" signal is missing. |
| **Translated site (e.g. Greek)** | Works normally — we compare the **raw source** (Greek) to last time's raw source. | We fingerprint *before* translation, so it's an apples‑to‑apples comparison. |
| **We improved the extraction instructions or switched AI models** | Every cached recipe is treated as stale → re‑read over time. | A code/model upgrade should propagate to all recipes; the cache key includes the instruction version + model. |
| **Nothing changed for a long time** | After **30 days**, we re‑read anyway (and check for changes). | A safety net so records never go stale forever. Lines up with our Moz‑score refresh cadence. |
| **The recipe came out thin/empty (JS‑heavy page)** | Retry once with a **full real browser** that runs the page's JavaScript. | Some sites load the recipe with JavaScript; a plain fetch sees only the shell. |
| **Site blocks us / is a paywall** | Try a paid "unblocker"; if the page is just a teaser ("sign up for full access"), **drop it**. | We won't save a 3‑ingredient teaser as if it were the recipe. |
| **The SEMrush file we expected is missing/renamed** | Look for the newest matching export in the same folder; if truly none, give a clear error. | Files get re‑downloaded with new timestamps; we tolerate that. |
| **Screenshot fails to capture** | The recipe still saves; the row is just marked "incomplete" and we retry the screenshot next harvest (**no AI cost**). | A missing screenshot shouldn't block the recipe or cost an AI call. |
| **The daily DISH refresh (a different job)** | Always re‑reads (it deliberately forces fresh). | That job has already paid for its inputs and wants the very latest; it's a separate path from the publisher harvest. |
| **A normal person extracting one URL in the form** | Uses the simple fast cache (reuse within 30 days); no change‑detection. | Change‑detection is a harvest feature; interactive extracts stay snappy. |

---

## 9. Three modes, side by side (for the curious)

The recipe‑reading function can run in three modes:

- **Normal (default):** If we have a fresh saved copy, reuse it immediately — don't even
  re‑download. Used by the interactive form. *Fastest.*
- **Revalidate:** Re‑download (cheap, thanks to the page cache), check the fingerprint, and
  reuse only if the recipe is unchanged. Used by the **publisher harvest**. *Smartest.*
- **Force‑refresh:** Ignore the cache and always re‑read. Used by the **daily dish
  refresh**. *Freshest, most expensive.*

> **Under the hood:** the `revalidate` and `force_refresh` flags on
> `extract_recipe_from_url`.

---

## 10. Why this design is good (the takeaways)

- **It saves the expensive thing (AI), not just the cheap thing (download).** Reusing a
  finished recipe is the real win.
- **It only re‑does work when the recipe truly changed** — not when a date or rating
  wiggled.
- **It still refreshes** — via change‑detection on every harvest, plus a 30‑day safety net,
  plus automatic re‑reads when we upgrade the pipeline. Records never get permanently
  stuck.
- **It's portable.** Nothing is hard‑coded to one computer; tunable settings live in a
  config table, and the system finds what it needs (like the screenshot browser) on its
  own.

If you understood the "smart student with notes" analogy at the top — keeps notes, re‑reads
only changed editions, ignores new covers — you understand the whole system.
