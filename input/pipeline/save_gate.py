"""The ONE save-quality gate.

Decides whether an extracted recipe is real enough to store. Three importers,
one function — this file exists because there used to be two hand-kept copies
(save_recipe_api._is_cacheable and intake.process_batch._batch_save_worthy)
whose "mirror" comment went stale the day the first escape hatch landed in
only one of them (2026-08-28: the rich-ingredient rule).

Callers pass their own floors:
  - the extract-cache layer uses the relaxed defaults (>=2 / >=2)
  - the POST /recipes save gate and the batch pre-filter pass the
    bcc_config.json thresholds (save_gate_min_* — default 3/3)

The escape hatches below are POLICY, independent of the caller's floors, and
each carries the measurement that justified it. Change them here and every
path moves together.
"""

# How much method text an UNENUMERATED single instruction must carry to count
# as a real method. Set at the lowest real example measured in the corpus
# (Thomas Keller's roast chicken, 157 chars) with a little room below it; the
# junk cases sat at 7 and 132 chars. See the corpus table in is_cacheable.
SINGLE_STEP_MIN_CHARS = 150
# The same floor for a method written in a dense script (CJK). Set from the
# measured ratio on the case that exposed it — 77 Chinese characters carrying
# what took 315 in English, ~4x — so 150/4 ≈ 40, kept at 40 rather than rounded
# down further because the junk cases were short on BOTH axes anyway (7 and 132
# characters with 1 ingredient, killed by the ingredient floor regardless).
SINGLE_STEP_MIN_CJK_CHARS = 40
# A short-method recipe with a RICH ingredient list is a no-cook/mix-only dish
# (coleslaw, spice blend, dressing, salad), not a failed extraction — none of
# the failure modes this gate guards (paywall stub, 404, sidebar-carousel
# wrong-node) can produce 5+ real ingredients. Measured twice on 2026-08-28:
#   * replaying every historical `skip-thin: fewer than 3 instructions` reject
#     still in llm_extract_cache: 10 flipped, all genuine (Cajun seasoning x4,
#     horiatiki, coleslaw, ginger dressing, overnight oats), 0 junk admitted;
#   * the first post-fix Cajun Seasoning run then rejected 11 MORE pages, all
#     "fewer than 3 instructions (1)" — probing their JSON-LD showed 10/11 are
#     real one-step spice blends with 7-9 ingredients (the 11th had 4
#     ingredients and stays out). Hence min steps = 1, not 2.
# The trigger case: spendwithpennies' 9-ingredient coleslaw, the #1-ranked
# candidate of its run, rejected for 145 chars of method (floor was 150).
RICH_INGREDIENT_MIN_INGS = 5
RICH_INGREDIENT_MIN_STEPS = 1


def is_cacheable(recipe: dict, *, min_ings: int = 2, min_steps: int = 2) -> tuple[bool, str]:
    """Refuse rows that look like a bad extraction (paywall, 404,
    picked-the-wrong-recipe sidebar carousel). Returns (ok, reason)."""
    name = (recipe.get("name") or "").strip() if recipe else ""
    if not name:
        return False, "no name"
    ings = recipe.get("recipeIngredient") or []
    real_ings = sum(1 for i in ings if str(i).strip())
    if real_ings < min_ings:
        return False, f"fewer than {min_ings} ingredients ({real_ings})"
    steps = recipe.get("recipeInstructions") or []
    real_steps = 0
    for s in steps:
        text = s.get("text") if isinstance(s, dict) else s
        if str(text or "").strip():
            real_steps += 1
    if real_steps < min_steps:
        # Rich ingredient list beats the step floor (see the constants above).
        # Checked before the prose floor so a terse "combine all ingredients"
        # method doesn't lose on character count.
        if real_steps >= RICH_INGREDIENT_MIN_STEPS and real_ings >= RICH_INGREDIENT_MIN_INGS:
            return True, (f"ok ({real_steps} step(s) but {real_ings} ingredients "
                          "— no-cook/mix-only recipe)")
        # A SINGLE SUBSTANTIAL PARAGRAPH IS A METHOD, NOT A FAILED EXTRACTION.
        # Counting steps assumes the publisher numbered them. Plenty don't:
        # m.xiachufang.com/recipe/107744561 ships its whole method as ONE string
        # in its own JSON-LD ("marinate 10 min ... wrap in foil ... 205C for 20
        # ... open foil, 5 more"), four real actions in one paragraph. We
        # reproduced it faithfully and then refused to save it.
        #
        # Measured over the corpus 2026-08-14 — exactly 6 of 5,593 rows have a
        # single instruction, and length separates them cleanly:
        #     7 chars / 1 ing   Pork Rice                     <- junk
        #   132 chars / 1 ing   'Parmesan Chicken | Recipes'  <- junk (title suffix
        #                                                        = wrong node)
        #   157 chars / 5 ing   Thomas Keller's Roast Chicken <- real
        #   239 chars / 7 ing   Spaghetti and Meatballs       <- real
        #   276 chars / 7 ing   Raita                         <- real
        #   315 chars / 6 ing   Air Fryer Garlic Pork Ribs    <- real
        # (median TOTAL instruction text on multi-step rows: 1,053 chars.)
        #
        # So: accept one step only when it carries real METHOD text. The
        # ingredient floor above has already run, which is what kills both junk
        # rows independently — this is deliberately belt-and-braces, because the
        # failure mode we are guarding is a paywall stub or a sidebar carousel,
        # and those are short.
        prose = 0
        cjk = 0
        for s in steps:
            text = str((s.get("text") if isinstance(s, dict) else s) or "").strip()
            prose += len(text)
            cjk += sum(1 for ch in text if 0x2e80 <= ord(ch) <= 0x9fff
                       or 0x3040 <= ord(ch) <= 0x30ff or 0xac00 <= ord(ch) <= 0xd7af)
        # A DENSE SCRIPT SAYS THE SAME THING IN FAR FEWER CHARACTERS, so a
        # character floor calibrated on English rejects an equivalent CJK method.
        # Measured on m.xiachufang.com/recipe/107744561: the identical method is
        # 77 characters in Chinese and 315 in English — a 4x difference in length
        # for the same four cooking actions. A recipe that survives translation is
        # judged on its English text and never reaches this branch; one saved
        # untranslated (curator choice, or a translation we declined) would be
        # refused for being written in Chinese, which is not a quality signal.
        floor = SINGLE_STEP_MIN_CHARS
        if prose and (cjk / prose) >= 0.30:
            floor = SINGLE_STEP_MIN_CJK_CHARS
        if real_steps >= 1 and prose >= floor:
            return True, (f"ok (single {prose}-char method paragraph; publisher "
                          f"did not enumerate steps)")
        return False, f"fewer than {min_steps} instructions ({real_steps})"
    return True, "ok"
