# The cognitive science of procedural instruction — a deeper report

**Companion to [`procedural-instruction-research.md`](procedural-instruction-research.md)** (the terse design-rules synthesis). This document explains the *thinking and studies* behind those conclusions: what each theory actually claims, how the key experiments were run, what the effect sizes mean, where the evidence is strong vs thin, and how each finding chains into a concrete BCC cook-view design decision.

---

## Provenance & honesty note (read first)

This report was assembled **without a new research run** (deliberately — no new fetch/spend). It draws on two sources, and I mark which is which throughout:

- **[VERIFIED]** — claims and effect sizes that came out of our own deep-research workflow `wf_77ce16a2` (2026-06-16: 23 sources, 25 claims, 3-vote adversarial verification), already recorded in the synthesis doc. These carry the vote outcome where it mattered (e.g. *refuted 0-3*, *medium 2-1*).
- **[LITERATURE]** — my explanation of the underlying, foundational cognitive-science work (Miller, Cowan, Sweller, Mayer, Baddeley, Kalyuga, etc.). These are well-established, decades-old results I'm describing from general knowledge to give the *why* behind the verified headline.

**Caveat:** because I did not re-fetch primary sources for this write-up, treat specific bibliographic details (journal, year, page) as *to-be-confirmed* before any published or marketing use. The conceptual claims are robust and standard; a stray date or exact d-value may need a citation check. Where a number came from *our* run rather than the literature, I say so.

---

## 0. The question, framed

We are not designing a *textbook* (instruction to learn from later). We are designing **instruction to act on, in real time, with the hands occupied and often only the ear available** — a cook mid-recipe. That changes which science applies. The learning-sciences literature on *multimedia instruction* and *cognitive load* is the right body of work, but its results have to be read through three modifiers that describe cooking almost perfectly:

1. **Divided attention** — the executor is doing a physical task while receiving instructions, so there is little spare capacity for rehearsal.
2. **Transient delivery** — voice is the natural channel for messy hands, and voice *vanishes*.
3. **Mixed content** — actions (verbs) interleaved with precise quantities (numbers, temperatures, times) that must be exact.

Almost every design rule below falls out of one of those three modifiers meeting a classic result.

---

## 1. The capacity of working memory — how much can be "held at once"

### 1.1 Miller (1956) — the famous "7 ± 2," and what it really said
George Miller's *The Magical Number Seven, Plus or Minus Two* is the most-cited and most-*mis*-cited number in cognitive psychology. **[LITERATURE]** Miller observed two things: (a) the **immediate memory span** — how many items people can repeat back — clusters around 7 for digits, somewhat fewer for letters/words; and (b) the **span of absolute judgment** — how many distinct levels of a single stimulus dimension (pitch, loudness) people can label reliably — also hovers near 7. His deeper point was actually about **chunking**: the span is fixed in *chunks*, not raw items, and we expand effective capacity by *recoding* several items into one chunk (e.g. "1-4-9-2" → "the year Columbus sailed"). Miller himself treated "seven" as a rhetorical hook, not a hard architectural constant.

**Why it's the wrong number to design to:** the 7-item span is measured under conditions that *allow* rehearsal and chunking (verbal, self-paced, full attention). A cook with busy hands has none of those affordances.

### 1.2 Cowan (2001) — "the magical number 4" **[VERIFIED]** + **[LITERATURE]**
Nelson Cowan's reconsideration measured *pure* storage capacity by **stripping away the things that inflate Miller's span** — preventing rehearsal, preventing chunking, and using tasks where items can't be grouped. Under those controls, capacity converges on **~4 (range 3–5)**. **[LITERATURE]** Methodologically this matters because it isolates the *attentional* store (the items the central executive can hold active at once) from strategic tricks layered on top.

**[VERIFIED]** In our run this was the headline finding (unanimous), and critically, the *converse* claim — "7 ± 2 is the constraint we should design around" — was **refuted 0-3**. The verifiers' reasoning: 7 is the rehearsal-aided ceiling, irrelevant to a hands-busy executor; the honest design target is the *lower*, attention-limited number, and lower still under divided attention.

**Design consequence.** We do **not** hardcode a magic number. We design to a *low* ceiling and, where attention is divided (the hands-busy cook), assume *fewer*. This is the origin of the **"≤ ~3 interacting elements per spoken sub-step"** heuristic — not because "3" is sacred, but because it sits safely under the 3–5 attentional capacity *with margin for divided attention*.

---

## 2. Cognitive Load Theory — the engine behind most of the rules

Cognitive Load Theory (CLT), developed by John Sweller and colleagues from the late 1980s, is the framework that converts "working memory is small" into actionable design. **[LITERATURE]**

### 2.1 Three kinds of load
- **Intrinsic load** — the *inherent* difficulty of the material, set by its **element interactivity** (below). You can't wish it away; you can only sequence it.
- **Extraneous load** — load imposed by *how* the material is presented (bad wording, "see the list above," split attention between a diagram and its legend). This is **pure waste** and the first thing good design removes.
- **Germane load** — the productive effort of building a mental schema. (Later CLT formulations fold this into a reallocation of freed-up capacity rather than a separate pot.)

The whole game: **drive extraneous load to zero, then sequence the intrinsic load so it never exceeds capacity at any instant.**

### 2.2 Element interactivity — the concept doing the real work **[VERIFIED]** + **[LITERATURE]**
This is the single most important idea for the sub-step engine. **Load is not the number of words, or even the number of steps — it is the number of elements that must be processed *simultaneously because they depend on each other*.** **[LITERATURE]**

- **Low interactivity:** elements that can be understood/done one at a time, in sequence. "Dice the onion." "Mince the garlic." "Heat the oil." Each is independent — you finish one, *release it from mind*, start the next. Three low-interactivity actions impose roughly the load of *one*, processed serially.
- **High interactivity:** elements that only make sense together and must be held at once. "Whisk constantly **while** slowly pouring in the hot stock **so it doesn't curdle**." You cannot decompose that without breaking it — the whisking, the pouring rate, and the reason are one irreducible unit.

**[VERIFIED]** Sweller (2010) framed element interactivity as the source of intrinsic load; our verifiers carried this unanimously and drew the explicit rule from it.

**Design consequence — the split rule.** *Split on independence, cluster on coupling.* An independent sequential action becomes its own sub-step (low cost, and segmenting it buys the benefits in §4). A tightly-coupled simultaneous action stays a single sub-step *no matter how long the sentence is*, because splitting it would manufacture extraneous load (the cook would have to mentally re-join what we tore apart). This is exactly the instinct "cluster-but-split, not naive sentence-split" — now with a name and a mechanism.

### 2.3 Two corollary effects we also exploit **[LITERATURE]**
- **Split-attention effect:** when two pieces of information must be mentally integrated but are *physically separated* (a step that says "add the spices" while the amounts live in a list above), the learner spends capacity just holding one while finding the other. → our **"no lookback"** rule: each step carries its own gear + amounts inline; nothing points away. This is extraneous-load removal, straight from CLT.
- **Redundancy effect:** *adding* information that isn't needed (re-explaining what the cook already knows, or narrating numbers already on screen) *hurts*, because it still must be processed and reconciled. → motivates the voice/screen division in §3 and the expertise fade in §6 (for an expert, our novice-level detail *is* redundancy).
- **Worked-example & pre-training effects** (background): novices benefit from having structure pre-built rather than constructing it under load. → our **mise/bundles**: pre-measuring and pre-grouping ingredients *before* the cooking starts removes a whole class of in-task load (measuring while the pan is hot). Mise en place is, in CLT terms, *pre-training + extraneous-load removal* made physical.

---

## 3. Two channels, not one — the Modality Principle and its boundary

### 3.1 Why spoken + visual beats visual-alone (the mechanism) **[LITERATURE]**
Two converging theories explain why putting the *action* in the ear and the *numbers* on the screen expands effective capacity:

- **Baddeley & Hitch's working-memory model:** working memory isn't one store but several — a **phonological loop** (verbal/auditory) and a **visuospatial sketchpad** (visual), coordinated by a central executive. They have *separate* capacities. Load everything through the eyes (read all the text) and you bottleneck one channel; split it (hear the words, see the diagram) and you use two channels in parallel.
- **Paivio's dual-coding theory:** verbal and visual information are encoded in distinct but linked systems; presenting across both yields richer, more retrievable representations.

**The modality principle (Mayer):** people learn better from spoken text + graphics than from on-screen text + graphics, because the on-screen-text version overloads the single visual channel.

**[VERIFIED]** Our run recorded the empirical magnitude: spoken-beats-on-screen-text in **53 of 61 tests, median d ≈ 0.76** — a solid, repeatedly-replicated effect. (This specific tally came from *our* synthesis; the underlying modality literature, e.g. meta-analyses by Ginns and others, is the source class.)

### 3.2 The crucial boundary — where "voice wins" stops being true **[VERIFIED, medium 2-1]**
This is the nuance that most "just narrate it" designs miss, and the reason our verifiers only gave the modality claim a **medium (2-1)** rating rather than strong. The audio advantage **shrinks and can reverse** when the material is:

- **long** (see the transient effect, §4),
- **complex / high element-interactivity,**
- **self-paced and re-scannable** (the reader could have re-read at will), and
- **dense with measurements and technical terms.**

That list is a near-perfect description of a recipe. So the honest reading is *not* "speak everything." It's: **voice for the short action prompt; screen for the exact content where audio fails** — the quantities, temperatures, times, and technical terms a cook must get precisely right and may need to re-check.

**Design consequence.** Our sub-step carries **two faces**: a terse spoken `voice` line (the verb, the object, the doneness cue) and a persistent on-screen `screen` line that holds the numbers. The ear is given the thing it's good at (a short imperative); the eye is given the thing the ear is bad at (precise data that must survive re-scanning).

---

## 4. The Transient Information Effect — the deepest reason voice ≠ screen

This is the finding that most directly shaped the engine, so it gets the most space. **[VERIFIED]** (Leahy & Sweller 2011/2016; Sweller 2012) + **[LITERATURE]**.

### 4.1 The core phenomenon
Speech is **transient**: each word is gone the instant it's spoken. Text is **persistent**: it sits on the page (or screen) and can be re-scanned at the reader's own pace. For **short** messages this doesn't matter — the whole message fits in the phonological loop, and the modality advantage (§3) wins. But as the spoken message gets **longer**, a failure mode appears: by the time the listener reaches the end, the **beginning has already decayed** from working memory, so they can't integrate the parts into a whole. With persistent text, you'd simply glance back. With speech, you can't.

The striking experimental result: past a certain length, the usual audio advantage **reverses** — **long narration becomes *worse* than the equivalent long on-screen text.** The very transience that helps short audio (offloading to a second channel) *hurts* long audio (no review possible). This is the **modality × length interaction**: modality's sign depends on length.

### 4.2 "Shorter is often still too long" **[VERIFIED]**
A second, humbling result our run captured: in studies that tried to fix overload by *trimming* the narration, **one reduction wasn't enough** — only a *second* round of shortening restored the benefit. Designers systematically *under*-cut. The practical rule: **segment voice below the intuitively-short threshold; if a spoken step *feels* about right, it's probably still too long — split again.**

### 4.3 Why this is the strongest argument for our whole architecture
The transient-information effect is the scientific bedrock under three separate decisions:
1. **Voice must be aggressively shorter than screen** — the screen is persistent (re-scannable), so it can hold the full detail; the ear cannot, so it gets the minimum.
2. **Check-to-advance / one sub-step per "next"** — by delivering one short transient chunk and *waiting*, we never let spoken content run long enough to decay before integration.
3. **Place-keeping ("where was I?")** — because speech leaves no trace, an interrupted cook has nothing to re-scan; we must *re-speak* the current chunk on demand to reconstruct the lost transient state.

---

## 5. The Segmenting Principle — the license for check-to-advance

**[VERIFIED]** (Mayer) + **[LITERATURE]**.

### 5.1 What it is and how it was shown
The **segmenting principle**: people understand a procedure better when it's broken into **small, learner-paced segments** than when delivered as one continuous stream. The canonical demonstrations used narrated animations of a process (e.g. how a system works) presented either continuously or in short segments the learner advanced with a "CONTINUE" button. The segmented, **user-paced** version won — the learner gets to finish processing one chunk before the next arrives, so essential processing never piles up beyond capacity.

**[VERIFIED]** Our run recorded the early canonical effect at **d ≈ 0.98** (a very large effect, from the best-case original demonstration) and a **durable meta-analytic range of ~0.5–0.8**, larger for *complex* material and *novice* learners. Honesty from the verification: a stronger packaging of the claim ("10/10 experiments at 0.79") was **knocked down 1-2**, but the **principle itself — segment + let the user pace — held 3-0** via the original demonstration. So we lean on the principle confidently and avoid over-citing a too-clean number.

### 5.2 Why "user-paced" is the active ingredient
It's not segmentation alone — it's segmentation **under the executor's control**. A fixed-timer slideshow doesn't help (it can advance before you're ready, or dawdle). The benefit comes from the *learner* signalling "next" when *their* working memory has cleared. This is a precise scientific endorsement of **check-to-advance**: the cook says "next" (or taps) exactly when they've finished the current action, and only then does the next memory-sized chunk arrive. We didn't invent check-to-advance and retro-fit a justification; the segmenting principle prescribes it.

---

## 6. The Expertise Reversal Effect — why one granularity can't fit everyone

**[VERIFIED]** (Kalyuga 2007) + **[LITERATURE]** (Kalyuga, Ayres, Chandler & Sweller 2003).

### 6.1 The finding
Instructional support that **helps a novice can actively hurt an expert.** The mechanism is the redundancy effect (§2.3) applied across skill levels: a novice lacks the schema, so detailed step-by-step guidance is essential scaffolding; an expert *already has* the schema, so that same guidance is **redundant information they must still read and reconcile** with what they already know — pure extraneous load. Empirically, the instructional condition that produced the best novice outcomes produced *worse* expert outcomes, and vice versa — the lines literally cross over as expertise grows.

### 6.2 Design consequence — expertise fade
There is **no single correct granularity.** The same recipe should present **verbose, finely-split** sub-steps to a beginner and **terser, more-clustered** sub-steps to an experienced cook (who'd find "dice the onion · mince the garlic · heat the oil" insultingly granular and would rather hear "build your aromatic base"). Hence the planned **verbosity preference / expertise fade**: a detail-level control (and, later, behavioral inference) that re-clusters sub-steps for skill. Until a skill model exists, a simple "less detail" setting is the honest first step — the science says *offer the axis*, even before we can auto-tune it.

---

## 7. Applied-practice traditions — convergent, but lower-evidence

The synthesis flagged these as **requested but not independently verifiable by a surviving primary source** in our run. I include them because they are real, battle-tested practitioner frameworks that **converge** on the same structure the lab science predicts — but I label them honestly as **convergent practitioner evidence, not proven causal claims.** **[LITERATURE, low evidence]**

- **Toyota TWI "Job Instruction"** (Training Within Industry, 1940s): the most striking convergence. TWI decomposes a job into **"Important Steps"** (what to do) each annotated with **"Key Points"** (the few make-or-break details — safety, quality, "knack") and **Reasons**. That is *exactly* our action-line / critical-detail split, arrived at independently by industrial trainers decades before CLT. The Key Point concept maps cleanly onto "the one number/cue the ear must not lose."
- **The surgical/aviation checklist** (popularized by Atul Gawande, *The Checklist Manifesto*): short, segmented, pause-point items that offload memory and enforce sequence under stress — segmenting + extraneous-load removal in a high-stakes hands-busy setting (the closest real-world analog to cooking).
- **Mise en place:** the culinary tradition of pre-staging every component before cooking. In CLT terms it is **pre-training + extraneous-load removal made physical** — exactly our bundles/mise (§2.3).
- **Carroll's minimalist instruction** (*The Nurnberg Funnel*): action-oriented, get-started-fast, error-tolerant documentation — minimize reading, maximize doing; aligns with "voice = short imperative."
- **IKEA-style wordless sequential diagrams:** one action per frame, numbered, language-independent — segmenting + modality (visual) for a manual task.

**Why they didn't "verify":** our 3-vote adversarial check demands a primary empirical source for a *causal* claim; practitioner frameworks offer strong face validity and long field use but rarely a controlled study isolating the mechanism. So we treat them as **corroboration and design vocabulary**, not as load-bearing evidence — and we flag a future research run specifically on TWI Key Points and place-keeping/prospective memory.

---

## 8. From science to the BCC cook-view — the mapping

| Design decision (shipped) | Primary finding(s) it rests on |
|---|---|
| **Split a step on independence; keep coupled actions together** | Element interactivity (§2.2); split-attention (§2.3) |
| **≤ ~3 interacting elements per spoken sub-step** | Working-memory capacity ~4 / divided attention (§1.2) |
| **`voice` = terse action; `screen` = the numbers** | Modality principle + its boundary (§3); transient information (§4) |
| **Aggressively short voice lines; "if it feels short, split again"** | "Shorter is still too long" (§4.2) |
| **One sub-step per "next" (check-to-advance, user-paced)** | Segmenting principle (§5) |
| **Coverage gate — every ingredient spoken in some sub-step** | Don't silently drop essential elements; faithful segmentation (§5) |
| **Mise / bundles — pre-measure & pre-group before cooking** | Pre-training + extraneous-load removal (§2.3) |
| **"No lookback" — each step self-contained** | Split-attention effect (§2.3) |
| **"Where was I?" re-speaks the current chunk** | Transience leaves no trace to re-scan (§4.3) |
| **Verbosity / expertise-fade preference (planned)** | Expertise reversal (§6) |

---

## 9. What the evidence does **not** settle (honest open questions)

- **Interruption & place-keeping (procedural prospective memory).** Central for cooks ("the doorbell rang — where was I?"), but **no primary source survived our verification.** Our current answer (check-to-advance state + highlight + re-speak the chunk) is *reasoned design*, not evidence-backed. Flagged for a dedicated follow-up run.
- **Applied-practice causal strength.** §7 is convergent, not proven. A targeted run on TWI "Key Points" and checklist mechanisms could promote some of it to verified.
- **The exact element cap.** "≤3–4" is a heuristic sitting under the 3–5 capacity band with a divided-attention margin — not a measured optimum for cooking specifically.
- **The transient crossover length.** The effect's *direction* is robust; the precise word/second count where audio flips from advantage to liability is material- and listener-dependent and we have not pinned it.
- **Individual differences & skill measurement.** Expertise reversal says granularity should adapt, but we have no validated in-app skill signal yet, so the adaptation is currently a manual setting.
- **Generalization beyond cooking.** The principles are domain-general (assembly, repair, clinical procedures), but our verification sampled the *learning-sciences* literature; transfer to, say, time-critical emergency procedures is plausible-but-untested here.

---

## 10. Key works referenced (confirm bibliography before formal use)

*General cognitive-science foundations — **[LITERATURE]**, standard works:*
- **Miller (1956)** — *The Magical Number Seven, Plus or Minus Two.* Memory span & chunking.
- **Cowan (2001)** — *The magical number 4 in short-term memory.* Pure capacity ≈ 4.
- **Baddeley & Hitch (1974)**; **Baddeley** — the multi-component working-memory model (phonological loop / visuospatial sketchpad).
- **Paivio** — dual-coding theory.
- **Sweller (1988, 1994, 2010)** and colleagues — Cognitive Load Theory; element interactivity; split-attention, redundancy, worked-example effects.
- **Mayer** — Cognitive Theory of Multimedia Learning; modality and segmenting principles.
- **Leahy & Sweller (2011, 2016); Sweller (2012)** — the transient information effect.
- **Kalyuga, Ayres, Chandler & Sweller (2003); Kalyuga (2007)** — the expertise reversal effect.
- *(modality meta-analyses, e.g. Ginns and others — the source class for the 53/61 tally)*

*Practitioner frameworks — **[LITERATURE, low evidence]**:*
- **Training Within Industry — Job Instruction** (1940s): Important Steps / Key Points / Reasons.
- **Gawande (2009)** — *The Checklist Manifesto.*
- **Carroll (1990)** — *The Nurnberg Funnel* (minimalist instruction).
- *Mise en place* (culinary tradition); IKEA-style wordless sequential instruction.

*Project-internal:*
- Our deep-research workflow **`wf_77ce16a2`** (2026-06-16): 23 sources, 25 claims, 3-vote adversarial verification — the source of every **[VERIFIED]** tag and vote outcome above.

---

*This report deliberately spends no new research budget. If we later want any of the **[LITERATURE]** or **[low evidence]** sections promoted to fully-verified with fresh primary citations — especially place-keeping/prospective memory and the TWI/checklist applied traditions — that's a scoped follow-up run, not a rewrite.*
