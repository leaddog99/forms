# How to present procedural instructions to a human executing them (research → design)

Deep-research synthesis (2026-06-16, workflow wf_77ce16a2 · 23 sources, 25 claims, 3-vote adversarial verification). The science behind BCC's cook-view step-breakdown — and a general procedural-delivery engine (recipe = procedure: parts/tools/steps/tips). Lead application: cooking, often hands-free by voice. See [[project_paid_pa_calibration]] sibling memories; this backs the [[project_marketing_differentiation]] "cognitively-grounded instruction" pillar.

## Verified findings (STRONG — primary sources, unanimous 3-0 unless noted)

1. **Working memory ≈ 4 chunks (range 3-5), NOT Miller's 7±2.** Cowan (2001) "magical number 4"; the "7" was a rhetorical estimate. The measured number is method-dependent (brief presentation ≈4; longer inflates via chunking). The claim that "7±2 is the constraint to design around" was **REFUTED 0-3**. → Don't hardcode a magic number; design to a *low* ceiling, *fewer* under divided attention (a cook can't rehearse with hands busy).

2. **Load = element interactivity** — how many elements must be held + integrated *simultaneously*, not raw word/verb count (Sweller 2010). → **The split rule:** independent sequential actions load little (split them apart); tightly-coupled simultaneous actions ("whisk constantly *while* pouring the hot stock") are one unit (keep together). Intrinsic = essential coupling; extraneous = bad wording/split references (strip it).

3. **Segmenting is the strongest lever** (Mayer): present a procedure in small, **user-paced** segments (~1-2 sentences each), not one continuous unit. Canonical demo d=0.98 (early best case; durable ≈0.5-0.8, bigger for complex material + novices). → **Direct scientific license for check-to-advance**: each sub-step memory-sized, the next arrives only when the cook says so. (The "10/10 experiments, 0.79" framing was killed 1-2; the *principle* via the original demo held 3-0.)

4. **Transient information effect — voice is fundamentally different from screen** (Leahy & Sweller 2011/2016; Sweller 2012). Speech *vanishes and can't be re-scanned*, so for **long** content the usual audio advantage **REVERSES** — long narration is *worse* than the equivalent on-screen text. → **Voice steps must be aggressively shorter than screen steps**; the persistent screen can hold more (the cook re-scans), the ear can't.

5. **"Shorter" is often still too long** — merely trimming audio failed; only a *second* reduction restored the benefit (modality × length interaction). → For voice, segment **below** the intuitively-short threshold; if a spoken step *feels* long, it is — split again.

6. **Modality "voice wins" is bounded** (MEDIUM, 2-1 vote). Spoken words beat on-screen text in 53/61 tests (median d=0.76) by splitting load across channels — BUT the advantage shrinks/reverses for *long, complex, self-paced, re-scannable* material with *measurements/technical terms* — which describes cooking almost exactly. → Voice for **short action prompts**; **screen** for the numbers (quantities, temps, times) and technical terms — the exact content where audio fails.

7. **Expertise reversal** (Kalyuga 2007): detail that helps a novice *harms* an expert (extra guidance they must reconcile with their own schemas = added load). → Step detail/granularity must **adapt to skill**: verbose + finely-split for novices, terser + more-clustered for experts.

## Concrete design principles for the sub-step engine (what to actually build)

The LLM/algorithm that splits a dense `_cook` step into sub-steps should:

- **Split on independence, cluster on coupling.** One sub-step = one action OR one tightly-coupled action-cluster. "Dice onion · mince garlic · heat oil" → 3 sub-steps. "Whisk while slowly adding the stock" → 1 sub-step. (This is element interactivity — and exactly the "cluster-but-split, not naive sentence-split" instinct.)
- **Cap interacting elements ≈3-4 per sub-step (≤3 for voice).** Count things the cook must *hold at once* (a temp + a time + a doneness cue), not words.
- **Dual rendering per sub-step:** a **terse spoken line** (the minimal action — "Whisk in the stock, a ladle at a time") + a **fuller screen line** that carries the measurements/temps/times. The ear gets the verb; the eye gets the numbers. (cook.html already shows amounts on screen — the voice should speak the *action*, not recite every quantity.)
- **Keep check-to-advance + user pacing** (we have it) — it *is* the segmenting principle.
- **Expertise fade:** a detail level (verbose ↔ concise) that clusters more per step for experienced cooks. No skill model yet → ship a setting / "less detail" toggle; behavioral inference later.

## Open gaps (research flagged — NOT answered by verified evidence)

- **Interruption / place-keeping ("where was I?")** — central for cooks, but no primary source survived verification (procedural prospective memory). Our check-to-advance + current-step highlight already place-keeps; a spoken resumption cue ("you're on step 4 — searing") is the likely design. **Candidate for a follow-up research run.**
- **Applied practice** (aviation/surgical checklists/Gawande, IKEA, Carroll minimalism, Toyota TWI "important steps + key points", mise en place) — requested but no dedicated primary source survived; answered only indirectly via the cognitive-load principles. Also a follow-up.

## Build status

- **v1 SHIPPED** (cook.html, client-side, works on existing `_cook`): `splitSpoken()` element-interactivity splitter (split independent / keep coupled `while|until|as`); voice `next/back/repeat` operate at **sub-step** granularity (back/repeat = the voice re-scan); on-screen "Step N · part i/m" cue; whole step stays visible (screen tolerates more). **Spoken mise** pre-step (`speakMise`) at fresh hands-free start + `mise` command — clusters laid out + measured ONCE by name. **`where was I`** command (`whereWasI`) — step N of M + current sub-step (place-keeping). Silent/screen mode (Talk off) = whole-step, unchanged.
- **v2 SHIPPED** (model-authored, 2026-06-16): the rework (`cook_rework` v2.2) emits a `substeps` split on each `_cook` step — `voice` (terse spoken action, the ear gets the verb) + optional `screen` fragment (the measurements, via `{ingN}`/`{amt}` tokens) + `ingredients` (parent indices, COVERAGE-gated so nothing goes unspoken). Split-on-independence / cluster-on-coupling + ≤3-element cap are prompt rules; a lenient `v_substeps` gauntlet gate guards integrity (fires only when substeps present → zero regression). The cook-view voice loop walks them (one sub-step per "next", back/repeat = re-scan, "part i/m" cue, where-was-I re-speaks the current chunk). Proven live on Chicken Milanese (7 steps → 15 sub-steps; split cutlet→slice/pound/season, dredge stayed one; voice "pound thin" / screen "⅓ inch").
- **On-screen sub-steps SHIPPED** (2026-06-16): the cook-view now renders each step AS its sub-step checklist (the `screen` fragment, else the spoken action), and the chunk being spoken **lights up live** (`.substep.active`) — the dual voice/screen rendering completed (ear + eye in sync). Steps without substeps still show the whole instruction (back-compat).
- **v2 follow-ups (planned):** sub-step the mise itself (check-to-advance per cluster); **verbosity preference** read at cook-page gen (verbose↔concise → expertise fade); follow-up research run on interruption/place-keeping + applied checklists/TWI.

## Reuse — it's a general procedural-delivery engine

None of the above is cooking-specific — it's about presenting *any* procedure to a human executing it. The same sub-step engine (split-on-independence, voice-short/screen-full, expertise fade, check-to-advance) applies to plumbing, assembly, repair. The `_cook` schema (ingredients/equipment/steps/tips) is a general procedure schema. This is both the build plan for sub-steps AND the differentiation story: a cognitively-grounded, cross-domain procedural-guidance platform, cooking-first.
