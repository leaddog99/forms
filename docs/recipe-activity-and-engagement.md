# Recipe Activity & Engagement — Design Note

**Status:** design only, nothing built. Written 2026-08-14.

Two instrumentation systems that look similar and must not be the same table:

| | **Activity log** | **Engagement log** |
|---|---|---|
| answers | *what did the SYSTEM do to this record?* | *what did PEOPLE do with this recipe?* |
| grain | one row per state change | one row per interaction |
| subject | a URL | a recipe + (maybe) a person |
| audience | operators, debugging | us, deciding what to build |
| retention | keep — it is provenance | bounded — see §5 |
| privacy weight | none (it is our own machinery) | **all of it** |

They share a shape and nothing else. Merging them buys one table and costs the
ability to reason about either — the activity log wants to be permanent and
boring, the engagement log wants to be aggregated and forgettable.

---

## 1. Why the activity log, concretely

Every defect found on 2026-08-14 had the same shape: **the system did something
and the record did not show it.** Nine bugs, and not one raised an error.

- **321 rows re-stamped** by the paywall calibration. Nothing on the rows says so.
- **7 rows deleted** by a publisher refresh's delete-and-replace, four minutes
  after they were created. No trace anywhere except a job log nobody reads.
- **3 rows saved with untranslated Chinese** while `_source.translated` said
  `True` — the field was stamped by a step whose output was then discarded.
- **45 screenshots failed nightly for six consecutive days.** Visible only by
  diffing six job results by hand.
- **Enrichment fired on 38% of master saves** (75 of 199) with no record of which.

Each of those is one line in a log we do not have. The pattern is not "we lack
logging" — the job logs are excellent. It is that **the logs are organised by
RUN and the questions are asked about a RECORD.**

---

## 2. The replace-vs-update problem, and why it settles the design

The obvious implementation — an array inside the recipe's `data` JSON — is the
wrong one, and the reason is decisive.

**Publisher and dish refreshes are DELETE-AND-REPLACE.** `retire_master_membership`
drops the publisher's rows; a dish refresh reports `deleted_prior_rows`. An
in-record log dies exactly when the most interesting thing happened to the
record. It cannot, even in principle, record its own deletion.

So: **a separate table, keyed on `url_normalized`, not on `recipe_id`.**

- `recipe_id` is regenerated on re-insert. `url_normalized` is not.
- `(url_normalized, user_id)` is *already* the adopt/dedup key the save path uses.
- Delete-and-replace becomes a legible event ("deleted by job 843, recreated by
  job 844") instead of an erasure.

This is not a new pattern here. Three url-keyed side tables already outlive the
rows they describe:

| table | rows | note |
|---|---|---|
| `metabase_url` | **7,102** | more URLs than there are recipes (5,594) — it already survives row churn |
| `run_candidates` | 2,534 | every URL a run considered, with stage/outcome/reason |
| `dish_rejects` | 519 | why a URL missed a dish |

`metabase_url` is the existence proof: it holds ~1,500 URLs whose recipe rows are
gone.

---

## 3. Activity log — schema

```sql
CREATE TABLE IF NOT EXISTS recipe_activity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_normalized  TEXT NOT NULL,
    user_id         INTEGER,          -- 0 master, N personal, NULL = not row-scoped
    event           TEXT NOT NULL,    -- controlled vocabulary, see below
    detail          TEXT,             -- one human sentence; NOT a blob
    job_id          INTEGER,          -- links back to the run that did it
    actor           TEXT NOT NULL,    -- 'job' | 'form' | 'bookmarklet' | 'system'
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_url ON recipe_activity(url_normalized, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_job ON recipe_activity(job_id) WHERE job_id IS NOT NULL;
```

**Vocabulary** — state changes only, never reads:

`extracted` · `saved` · `adopted` (update of an existing row) · `enriched` ·
`scored` · `unscored` (with reason) · `screenshot_captured` ·
`screenshot_refused` · `screenshot_latched` · `image_cooped` · `restamped`
(which job, which field) · `deleted` (which job, why) · `cook_reworked` ·
`translated` · `claimed`

**Write at chokepoints, not call sites.** The clearest lesson of 2026-08-14:
`_skip_auto_enrich` and `skip_jsonld_fast_lane` both failed because they relied
on every caller remembering. Four write points cover six of the nine bugs:

1. `_save_recipe_core` — saved / adopted / enriched / unscored
2. `store_screenshot_blob` + the refresh job — captured / refused / latched
3. `paywall_calibration.restamp_recipes` — restamped
4. `retire_master_membership` — deleted

**Non-fatal, always.** One INSERT, wrapped, never able to fail a save. Same rule
as the DA stamp and the publisher hint.

**Cap the display, not the table.** A few hundred bytes per event against 5,594
recipes is nothing, and the moment you most want history is after something
unusual happened *repeatedly*. Show the last 50 in the form; prune at 200/URL
only if it ever bites.

---

## 4. Engagement log — what we are actually trying to learn

The product questions that currently have no data behind them:

- Which recipes get **captured** (the conversion moment), and from which surface?
- Does the **enriched story** move capture rate? (It is our biggest per-recipe
  cost — see [[project_monetization_pipeline]].)
- Which recipes earn **cookbook inclusion** — the strongest signal a recipe is
  worth having, and unlike a click it is deliberate.
- Do **claims** cluster on particular dishes, publishers, or authority bands?
- Which **click-outs to the source** happen, since attribution is the deal we
  make with publishers.

These are decisions about what to build, not about who anyone is. That
distinction is the whole design.

And the one this exists to serve: **what does THIS user cook?** The engagement
log is the raw material for a per-user taste profile — the thing that makes
"here are the best, ranked" into "here are the best *for you*."

```sql
CREATE TABLE IF NOT EXISTS recipe_engagement (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_key      TEXT NOT NULL,    -- url_normalized, same key as activity
    user_id         INTEGER,          -- the actual user; NULL only for anonymous
    event           TEXT NOT NULL,    -- view | capture | cookbook_add | claim | source_click | cook_start | cook_complete
    surface         TEXT,             -- list | card | recipe | cook | search
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engagement_user ON recipe_engagement(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_engagement_recipe ON recipe_engagement(recipe_key, event);
```

### 4a. The profile is the product, and it already has its machinery

The profile is not a table of counters bolted on later — most of what it needs
already exists:

- **`_identity` cards** on every recipe: cuisine, ethnicity, technique, serving
  form, primary ingredients ([[project_identity_card]]).
- **Embeddings** on recipes and dishes, with a proven cohort matcher.
- **Chapters** and `dishes.chapter` for coarse structure.

So a taste profile is derivable rather than invented: **the centroid of the
embeddings of what a user captured and cooked**, plus counts over identity-card
facets. That gives, per user, an honest answer to "Sichuan and Levantine, heavy
on braises, avoids baking, 30-minute weeknights" — computed from what they
actually kept, not from a questionnaire.

Weighting matters and should be explicit: **cooking it > capturing it > viewing
it.** A capture is deliberate; a view is often an accident. `cook_complete` is
the strongest signal in the system and currently is not recorded at all.

Second-order uses, in rough order of value: ranking a dish's top-10 for this
user; choosing what to surface on the home surface; deciding which cookbooks to
suggest; and — the monetization link — which product categories are worth
building for this audience ([[project_monetization_pipeline]]).

---

## 5. Yes, we are building a user profile — the line is elsewhere

The curator has spent years arguing against industry data collection and is now
specifying a system that profiles users. That is not a contradiction, but the
resolution has to be stated precisely or it becomes one.

**The objection was never to knowing things about a user.** A cooking product
that does not learn you like Sichuan braises and hate baking is a worse product.
The objection is to a specific bundle of practices — and every one of them is
separable from the profile itself:

| the objectionable thing | why it is objectionable | our line |
|---|---|---|
| the profile is **secret** | the subject cannot see, correct, or argue with it | **it is a visible page.** The user reads their own profile, and it says what it thinks in words: "you cook Sichuan and Levantine, mostly braises, rarely bake." |
| it serves **someone else** | built for an advertiser; the user is the inventory | **it serves them.** Its only consumers are their ranking, their suggestions, their cookbook. |
| it is **inescapable** | no meaningful opt-out, no deletion | **delete and export are first-class**, not a support ticket. Deleting it degrades their recommendations and nothing else. |
| it **follows them** | cross-site identifiers, data brokers, ad-tech SDKs | **first-party only, never leaves the instance.** No third-party pixels. Ever. |
| it is **hoarded** | collected because storage is cheap, kept forever | **every event names the decision it informs.** If nobody can say which choice a field changes, it is not collected. |
| it is **sold** | the actual business model | **never.** The business model is membership ([[project_monetization_pipeline]]) — the user is the customer, which is exactly what makes the profile legitimate. |

The short test: **would we show the user their own profile, in full, without
embarrassment?** If yes, it is the good kind. If any field would have to be
hidden to avoid an awkward conversation, that field is the problem — not the
profile.

**Time-of-day is the one field to think hardest about.** Cooking times are
genuinely useful (weeknight-fast vs weekend-project) and simultaneously a
schedule of someone's life. Prefer deriving the useful part — a `weeknight` /
`weekend` flag on the recipe — over storing timestamps precise enough to
reconstruct when someone is home.

**The product shape forces the good version anyway.** BCC is meant to be
self-hostable ([[project_portable_package]]) — someone else runs it, on their
box, for their users. A profile that assumes a central operator harvesting
across tenants is a design we would have to tear out to ship the product we say
we want. Instance-local is not a concession; it is the only version that
survives contact with the roadmap.

**Where this genuinely gets hard, unresolved:** aggregate insight across users
("people who capture X also capture Y") is how recommendations get good, and it
is a *cross-user* computation over per-user data. It is defensible — it is still
our instance, still serving those same users — but it is the first step onto the
path the objection is about, and it should be taken deliberately, written down,
and bounded (aggregate outputs only, no per-user export, minimum cohort sizes)
rather than discovered later in a migration.

---

## 6. Open questions

1. **Anonymous views.** Signed-in users are the profile; anonymous traffic is the
   acquisition channel and has no account to attach to. Counting it needs *some*
   dedup or every refresh is a view. A rotating per-day salted hash of IP+UA gives
   a daily-unique count with no durable identifier — but it is still a fingerprint
   for 24 hours. Alternative: do not count anonymous views at all and accept a
   blind spot on the free layer. Not resolved.
2. **When does the profile get computed?** On write (cheap per event, drifts if
   the embedding model changes) or on read (always current, costs a scan). The
   dish matcher already faces this and chose stored-with-restamp
   ([[feedback_persist_derived_values]]) — that precedent probably applies.
3. **Does the activity log get its own UI, or a panel in the recipe form?** A panel
   is cheaper and puts history where the record is. A cross-record view answers
   "what did job 843 touch?", which the `job_id` index supports either way.
4. **Backfill.** Today's events are gone. The log starts empty and only gets
   interesting after a few weeks — worth knowing before judging it.
5. **Does `retire_master_membership` know WHY it deleted a row?** It knows the job.
   The reason ("publisher refresh delete-and-replace") is inferable from the job
   type, and inferring is the kind of thing this log exists to stop doing.

---

## 7. What I would build first

The activity log, the four chokepoints, and a read-only panel in the recipe form.
It has no privacy surface, it pays for itself the first time a record is wrong,
and it would have caught six of nine bugs from a single day.

The engagement log should wait for the production display surface, because until
recipes have a real page there is nothing to instrument — and its schema should
be settled *before* that page ships, not retrofitted after.
