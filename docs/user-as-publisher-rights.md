# User-as-publisher — the content-rights / liability model for user cookbooks

**Status: DESIGN (2026-06-18).** The rights/liability posture for letting users publish their own cookbooks ([[project_cookbooks]]) using our editor + UI generators. Lives on the **BCC / possess-and-publish side**, deliberately separate from the corpus's discover-and-link posture ([[project_discover_vs_possess]]). Grounded in US recipe-copyright law + the DMCA §512(c) host model.

> **Not legal advice.** This captures the correct *structural shape* so the product is designed defensibly from day one. A commercial UGC-republish product is exactly the case where an IP/platform attorney must bless the ToS, the DMCA process, the generator design, and especially the monetization structure **before** scaling. The principles below are well-settled; the edges are where counsel earns its fee.

---

## 1. The core legal facts (why this is even possible)

- **A list of ingredients is NOT copyrightable.** A "simple set of directions" is NOT copyrightable. The US Copyright Office treats these as facts/procedure.
- **What IS protected:** the creative expression wrapped around the recipe — headnote/narrative **prose**, **photos**, the *specific literary wording* of instructions ("substantial literary expression"), and the *compilation* (a cookbook's selection + arrangement).
- **Consequence:** you can legally publish an original re-expression of a publicly available recipe **without any license** — you're not buying the recipe (nobody owns it); a license only ever buys *assets + goodwill* (their photos, prose, brand). Myths to kill: the "change 3 ingredients" rule is legally meaningless; attribution/link-back is an ethical/relationship norm, not a legal requirement.

Sources: [Copyright Alliance — recipes & cookbooks](https://copyrightalliance.org/are-recipes-cookbooks-protected-by-copyright/) · [Recipe copyright: what's protected](https://bootstrapped.ventures/recipe-copyright/) · [Nolo — protecting a recipe](https://www.nolo.com/legal-encyclopedia/can-you-protect-a-recipe.html).

---

## 2. The model: user is the AUTHOR, we are the HOST + toolmaker

The same architecture every UGC platform uses (YouTube / Allrecipes / Substack) — **DMCA §512(c) safe harbor**:
- The **user** authors + publishes the cookbook; the resulting work (their words, their/licensed images, their selection + arrangement) is **their** copyrightable expression.
- **We** supply (a) the unprotectable layer — ingredients + de-expressed procedure — and (b) tools (text-augment generator, licensed/own image insertion). We host; we don't author the protected layer.
- **ToS** makes the user warrant authorship + indemnify us.

This shifts and **shrinks** liability — it does **not** fully transfer it. Indemnification is a deterrent + cost-allocation tool, not armor: a rights-holder can still sue us directly, and most users are judgment-proof so the indemnity is paper. **The real shield is operating as a legitimate, non-inducing host** (§4), not the ToS clause.

---

## 3. The three "clean-supply" invariants (break one → liability walks back to US)

The shift only holds if what the *system* supplies is genuinely clean. If WE are the source of the protected expression, the user-as-author framing does not cover us — we become a direct/contributing infringer regardless of whose name is on the cookbook.

1. **Steps must be DE-EXPRESSED, never verbatim.** Hand over the *functional procedure, re-authored* — not a publisher's exact instruction wording (which can carry the protected literary layer). **Our `_cook` rework already does this** ([[project_recipe_anchor]]) — it normalizes + re-expresses the procedure into our own structured form. That is a genuine compliance asset, not incidental. A raw "their steps, copied" hand-off is the UNCLEAN version and must never be the supply.
2. **The augment-text generator must produce ORIGINAL prose, not paraphrase a source.** Original generated text = brand-new expression (fine). A close paraphrase of a source's headnote = a **derivative work the system generated** (not fine). Design the generator to *write fresh*, not to "rewrite theirs." It must never be fed, or steer the user to paste, a source's prose as the thing to reword.
3. **Images must be cleared for THIS downstream use, or be the user's own.** Most stock/source licenses do **not** permit sublicensing to end users who republish in their own (possibly monetized) product. "We have a license" ≠ "our users may republish it." Safe lanes: the **user's own images** (clean), or sources explicitly licensed for downstream republication. Maps onto the tiered [[project_image_policy]] — user's-own = full use is the safe lane; a source's hero stays corpus-only attributed thumbnail, never piped into a user's published cookbook.

---

## 4. Host-side obligations (the actual shield)

- **Register a DMCA agent**, run a **takedown process**, maintain a **repeat-infringer policy**.
- **Do not *induce* infringement.** A UI that nudges "paste the original recipe text here to reword" risks losing safe harbor (the inducement problem). The generators must steer toward original/licensed content **by design** — this is a product-design constraint, not just a policy.
- **Monetization prong (the careful part).** §512(c) safe harbor weakens when the host has **the right to control + a direct financial benefit** from specific infringing content. A revenue-share on user cookbooks blurs the "user is the publisher" line — courts weigh who controls and who profits. Doable, but this is the structure to run past counsel before turning on.

---

## 5. How it fits the architecture

- **BCC / possess side only.** This is a publish surface; keep it off the corpus, which stays discover-and-link ([[project_discover_vs_possess]], [[project_split_architecture]]). Conservative engine stays conservative.
- **Editor + cookbook** ([[project_cookbooks]]) is the publishing tool already built for exactly this; user-as-author is what it was *for*.
- **Two existing systems make the "clean supply" real rather than aspirational:** the rework (de-expression, §3.1) and the tiered image policy (§3.3). We half-built the compliance story without framing it as one.
- Relationship to supply models (from the 06-18 discussion): **C = rewrite-from-public is the engine** (our rework is the original work, no license needed); **B = selective publisher partnerships** only where we specifically want their *photos* or *brand halo*; **A = Allrecipes-style free UGC** fights our quality thesis. "Pay for republish rights" is the wrong frame — you can't buy what nobody owns.

---

## 6. Production economics — the rework is the cost AND the defensibility

The transformation that makes the output legally ours (§1, §3.1) is the expensive part. That tension is real but far more reducible than it first looks. Cook-rework runs **~$0.45 cold / ~$0.27 warm per recipe** on Opus 4.8 (measured, Beet-Cured Salmon; [[project_recipe_anchor]]) → a naive 100k ≈ $26.5k. The headline number is a worst case that never happens.

**Reframe first (the denominator):** the expense *is* the defensibility — a cheap paraphrase looks derivative; the substantial `_cook` re-plan is what we can publish without anyone's permission. It's **capex per recipe, paid once** (the `_cook` is persisted; re-paid only on a prompt-version bump or a user edit) and **shared across surfaces** (the engine is surface-agnostic — one rework serves TBOTB + BCC). So the number that matters is **cost-per-*published*-recipe vs lifetime-revenue-per-published**, not cost-per-crawled. A published recipe earning ad/affiliate/subscription revenue over its serving life clears $0.10–0.45 many times over.

**The cost-reduction ladder (ranked by impact):**
1. **Only rework what you PUBLISH** (biggest lever, not a model knob) — the corpus already scores/selects; only dish winners / editors' choice get reworked. Spend scales with the curated published subset (top-N per dish), a tiny fraction of crawl. The $26.5k/100k scenario assumes reworking the whole crawl — you never do.
2. **Batch API — flat 50% off, available now.** The rework is *already* an out-of-process job (restart-survivable, not real-time) = exactly Batch API's sweet spot (async, most batches <1hr). $0.45 → ~$0.23 for free. Lowest-effort win.
3. **Sonnet-first, Opus-on-failure** (the planned opus→sonnet 2-pass): Sonnet 4.6 is ~40% cheaper; the **gauntlet is the guardrail** — run the cheap model first, let the 10 gates + repair loop catch defects, escalate to Opus only on failure. Blended cost slides toward the cheap tier's base.
4. **Prompt caching — already active** (the $0.45→$0.27 warm gap *is* the cache): keep the big static prompt (rules/schema/gauntlet) frozen + first, per-recipe content last, ≥4k-token prefix (Opus 4.8 minimum). Warm across a whole batch.
5. **Haiku for sub-tasks** ($1/$5) where judgment isn't needed (not the rework itself).

Grounded pricing (per 1M tokens, 2026-06-04): Opus 4.8 $5/$25 · Sonnet 4.6 $3/$15 · Haiku 4.5 $1/$5 · Batch −50% · cache read ~0.1× / write 1.25×. **Stacked** (Batch × Sonnet-first × caching), the bulk that Sonnet clears lands ≈ **$0.10–0.15/recipe, one time**; full Opus cost is reserved only for gauntlet kick-backs.

**Cheaper / open / local models — viable BECAUSE of the gauntlet.** The 10-gate validator + repair + escalation means a weaker model can be tried as a new cheapest tier *in front of* Sonnet→Opus, with adoption gated empirically on **pass-rate × escalation-rate × judgment-quality** on a real sample (same discipline as the SERP fidelity A/B). Three buckets:
- **Local open-weight (Qwen/Llama/GLM) = the strategic fit**, not just cost — aligns with [[project_portable_package]] + BYOK: a self-hoster runs it on their own GPU (no keys, no per-token cost, no data leaving the box). Turns per-token into fixed cost; "bring your own local model" extends BYOK.
- **Chinese hosted APIs (DeepSeek-class) = cheapest quick win** (~5–10× under Sonnet) for the **corpus** rework (public recipes, low data-sensitivity). **Firewall user-private cookbooks (§2/§3) from any offshore model** — that's a different data calculus.
- **Keep Opus/Sonnet on the published flagship.** The gauntlet checks *mechanical integrity only* — it does NOT catch a valid-but-clunky split (bad judgment passes), so the brand-defining cook-view stays on a strong model; cheap/local earn their way up by pass-rate.
- **Real technical gate:** strict forced-tool / schema reliability + multi-constraint instruction-following (where frontier Claude still leads); a high defect rate pays itself back in repair-loop tokens + escalations. **Gateway wrinkle:** `llm.py` mirrors the Anthropic SDK; most cheap/local models are OpenAI-compatible → needs a provider abstraction (a clean fit for the pluggable-backend portable vision anyway).

---

## 7. Build/compliance checklist (when picked up)
1. ToS: user warrants authorship of added expression + grants us a host license + indemnifies; clear "you are the publisher" language.
2. DMCA: registered agent, takedown endpoint, repeat-infringer policy.
3. Generator design: text-augment writes original prose, never reword-a-pasted-source; no UI affordance that invites pasting protected text.
4. Image pipeline: only user's-own or downstream-cleared sources reach a *published* user cookbook; corpus heroes firewalled out ([[project_image_policy]]).
5. Supply: cookbook builder receives ingredients + **de-expressed** `_cook` steps only — never verbatim source instruction prose.
6. Monetization: defer revenue-share design until the control + financial-benefit prong is reviewed by counsel.
7. **Gate:** IP/platform attorney review of 1–6 before commercial launch.
