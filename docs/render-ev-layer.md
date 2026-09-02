# The render/EV layer — serving products beside recipes

*Design settled 2026-09-02. Not yet built. This is the last unbuilt link in the revenue
chain: recipe → dish → approved classes → curated picks → **this layer** → /go/ click →
commission.*

## Two layers, not one — the ad-platform decomposition

The curator asked for ad-style dynamic selection ("more along the lines of Facebook and
Google and YouTube advert selection... at worst some revenue/likelihood randomization
scheme"). That is the design — with the same decomposition every ad platform uses, because
the two decisions have different owners and different clocks:

**Eligibility (the chip gate) — what CAN be shown.** Facebook doesn't algorithmically
invent ad inventory; advertisers submit, policy review approves, and only then does the
auction rank. Our approve/reject on dish-class chips is that policy layer: a one-time
catalog decision per dish×class ("Beef and Broccoli may sell woks — forever"), the
sellability boundary, and the gate that mints registry classes. It is deliberately human
after the August auto-mint mess. It is NOT the layer that decides what a reader sees.

**Serving (this layer) — what IS shown to this reader, on this page, now.** Per
impression, pick ≤3 of the dish's approved classes for the shelf, each rendered as its
collection's top pick (image, one-line why, price, /go/ buy link). This is the auction
analog, and it is where the dynamic selection lives.

Relaxation on record for the chip gate: once the four-channel matcher's precision is
proven over a few dozen dishes, move to "auto-approve above a confidence bar, spot-check
below" — a one-line policy change that shrinks curator load without giving up the gate.

## The serving score

    EV(class, context) = p(click | context) × p(purchase | click) × price × rate

Inputs that exist today: dish-side (channel, tier, scope, the model's NEED order — already
defined as ownership-gap × dish-demand, which is a hand-built p(click) prior), and
commercial-side (typical_price on the pick, rate + status on the store's active program,
stamped at click by /go/). Reader-side signals do not exist yet — the activity-log →
taste-profile design (project_recipe_activity_engagement) is unbuilt — so at launch the
context is (dish, placement), not (dish, placement, reader).

## Phases — chosen for the traffic we actually have

Google's rankers learn from millions of impressions a day; a learned model at our launch
volume would just be noisy ESTIMATES wearing a lab coat. But — curator's point, on record
2026-09-02: "noise can be valuable" — noisy SERVING is a different thing and a feature at
every volume. Deliberately showing something other than the current best guess is the only
source of counterfactual data, it prevents the self-fulfilling loop where the first-shown
class wins forever merely because it was shown, and it occasionally surfaces the class
nobody predicted. That is why Phase 2 is Thompson sampling rather than
always-play-the-leader: structured noise, wide while uncertain, narrowing as evidence
lands — never zero. Even Phase 3 keeps an exploration floor (an epsilon of impressions
served off-policy) so the learned model can never fully lock in its own priors. The phases
below each work at the volume that exists when they ship, and each one's serving is also
the next one's data collection.

**Phase 0 — prerequisite, before any serving code.** The class↔collection join is a name
string today, and an impressions log would bake those strings into append-only rows. Give
curated_collections a real FK to product_classes first (docs/m2m-migration-map.md). Same
for the impressions/clicks tables: class ids, not names.

**Phase 1 — deterministic EV, plus the log.** Order the dish's approved classes by
need-order prior × rate × price; serve the top 3; write one append-only impression row per
slot (recipe, dish, class, collection, slot, ts). No learning yet — but the log is the
training set for everything after, and /go/ clicks already land in affiliate_clicks with
store and rate stamped. Ship this with the first render.

**Phase 2 — Thompson sampling over each dish's approved classes.** The curator's "at
worst" case is actually the correct next step, not a consolation prize: a per-(dish,class)
Beta posterior on CTR, seeded from the need order so day one behaves like Phase 1, sampled
per impression so exploration is principled rather than uniform randomization. Bandits are
the small-sample tool — they work honestly on tens of impressions, converge as volume
grows, and never need a training pipeline. Amazon-rate vs high-rate-store offers can join
the reward signal here (realized EPC per class, not just clicks).

**Phase 3 — when volume arrives (the teaser).** The moves that become worth making once
the impressions log is thick enough that splits stay significant:

- **Learned p(click)** replacing the Beta tables: features = dish, chapter, class family,
  channel/tier, slot position, season (canners spike in August, roasting pans in
  November). A small GBM over the log, refreshed weekly, nothing exotic.
- **The reader enters the context.** Taste profile from the activity log: suppress classes
  the reader has already clicked through to buy (the ownership-gap logic going dynamic —
  bought the wok? show the carbon-steel spatula next, not the wok again), boost families
  they engage with, cold-start on dish alone.
- **Offer-level EV.** Same product, multiple stores: route the buy button by realized
  earnings-per-click per store, not the quoted rate — quoted 10% with a broken checkout
  loses to quoted 3% at Amazon conversion.
- **Placement economics.** Frequency caps per reader×class, slot-position effects
  measured rather than assumed, and per-publisher pacing if a store's program has caps.

## What never changes, at any volume

The editorial firewall holds at every phase: EV chooses which CLASSES get the shelf and
which OFFER gets the button — it never reorders the picks INSIDE a class, which stay
ranked by review evidence alone. Buy links stay clean-stored and click-minted (/go/);
harvested links are never republished; the Editor's Choice slot keeps its curator-selected
label. The moment readers can't trust the list, the list stops converting — the firewall
is the asset the EV layer monetizes, not an obstacle to it.
