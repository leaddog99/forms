"""Proof for cook_validators: a GOOD _cook passes all 8 gates, and one planted
defect per gate provably fires it. Run: python test_cook_validators.py
"""
from cook_model import (
    CookMetadata, CookIngredient, CookAmount, FormVariant, Bundle, BundleMember,
    CookEquipment, CookStep, StepIngredient, StepEquipment, ReservedItem, CookSubStep,
)
from cook_validators import run_all


def make_good() -> CookMetadata:
    """A small internally-consistent vongole-ish rework that passes every gate."""
    amt = lambda i, m: CookAmount(imperial=i, metric=m)
    return CookMetadata(
        recipe_yield=2,
        ingredients=[
            CookIngredient(id="ing_pasta", name="spaghetti", first_step=1),
            CookIngredient(id="ing_clams", name="fresh vongole", first_step=2),
            CookIngredient(id="ing_garlic", name="garlic", first_step=2, shopping_hint="3 cloves"),
        ],
        bundles=[
            Bundle(id="b_ready", label="Measured & ready", first_step=1,
                   combine_note="everything pre-portioned",
                   members=[
                       BundleMember(ingredient_id="ing_pasta", amount=amt("8 oz", "225 g"), label="spaghetti"),
                       BundleMember(ingredient_id="ing_clams", amount=amt("1 lb", "450 g"), label="clams"),
                       BundleMember(ingredient_id="ing_garlic", amount=CookAmount.same("2 Tbsp minced"), label="garlic"),
                   ]),
        ],
        equipment=[
            CookEquipment(id="eq_pot", name="large pot", size_matters=True, size=amt("6 qt", "5.7 L"), category="pot", first_step=1),
            CookEquipment(id="eq_pan", name="saute pan", size_matters=True, size=amt("12 in", "30 cm"), category="pan", first_step=2),
        ],
        reserved=[
            ReservedItem(id="res_water", label="reserved pasta water", amount=amt("1/2 cup", "120 ml"),
                         from_ingredient_id="ing_pasta", created_step=1, consumed_step=2),
        ],
        steps=[
            CookStep(number=1, name="Boil the pasta",
                     instruction="Boil the {ing1} to al dente; reserve the water.",
                     ingredients=[StepIngredient(ingredient_id="ing_pasta", amount=amt("8 oz", "225 g"), label="spaghetti", definiteness="the")],
                     equipment=[StepEquipment(equipment_id="eq_pot")],
                     substeps=[
                         CookSubStep(voice="Boil the spaghetti to al dente.", ingredients=[1]),
                         CookSubStep(voice="Scoop out and reserve a half cup of the water.", ingredients=[]),
                     ]),
            CookStep(number=2, name="Steam the clams",
                     instruction="Add the {ing1} and the {ing2}; cover until they open.",
                     ingredients=[
                         StepIngredient(ingredient_id="ing_clams", amount=amt("1 lb", "450 g"), label="clams", definiteness="the"),
                         StepIngredient(ingredient_id="ing_garlic", amount=CookAmount.same("2 Tbsp minced"), label="garlic", definiteness="the"),
                     ],
                     equipment=[StepEquipment(equipment_id="eq_pan")],
                     substeps=[
                         CookSubStep(voice="Add the clams and garlic.", ingredients=[1, 2]),
                         CookSubStep(voice="Cover until they open.", ingredients=[]),
                     ]),
        ],
    )


def main():
    ok = True

    good = make_good()
    rep = run_all(good)
    print(f"GOOD fixture: passed={rep.passed} ({len(rep.ran)} gates ran)")
    if not rep.passed:
        ok = False
        for f in rep.failures:
            print("   unexpected:", f)

    # One planted defect per gate; each must fire ITS gate.
    cases = []

    g = make_good(); g.steps[0].ingredients[0].amount.imperial = "8 oz (225 g)"
    cases.append(("unit-consistency", g))

    g = make_good(); g.steps[1].ingredients[0].definiteness = "a"
    cases.append(("definite-article", g))

    g = make_good(); g.steps[0].ingredients[0].amount = CookAmount(imperial="a handful", metric="a handful")
    cases.append(("every-measure-numeric", g))

    g = make_good(); g.steps[0].instruction = "Boil the pasta — see the list above for amounts."
    cases.append(("no-lookback", g))

    g = make_good(); g.ingredients.append(CookIngredient(id="ing_orphan", name="parsley", first_step=2))
    cases.append(("mise-complete", g))

    g = make_good(); g.ingredients[0].first_step = 9  # now 9,2,2 -> descending
    cases.append(("appearance-order", g))

    g = make_good()
    g.steps[1].ingredients.append(StepIngredient(ingredient_id="ing_pasta",
        amount=CookAmount(imperial="8 oz", metric="225 g"), label="spaghetti", definiteness="the"))
    cases.append(("reuse-referenced", g))

    g = make_good(); g.bundles[0].combine_note = None; g.bundles[0].excluded_reason = None
    cases.append(("bundling", g))

    # substeps: drop ingredient 2 from step 2's coverage -> garlic left unspoken.
    g = make_good(); g.steps[1].substeps[0].ingredients = [1]
    cases.append(("substeps", g))

    for gate, cook in cases:
        rep = run_all(cook)
        fired = any(f.startswith(f"[{gate}]") for f in rep.failures)
        print(f"  defect -> {gate:22s}: fired={fired}")
        if not fired:
            ok = False
            print("     FAILURES SEEN:", rep.failures or "(none)")

    print("\nRESULT:", "ALL GOOD" if ok else "PROBLEMS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
