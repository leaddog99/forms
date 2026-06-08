"""
example_vongole.py — the prototype recipe expressed as a RecipeDoc.

Lets you render the page with no API key (validates models + template), and serves as a
golden fixture for evals. Run:  python -m recipe_anchor.example_vongole > out.html
"""
from .models import (Amount, Ingredient, Equipment, Step, StepIngredient,
                     StepEquipment, RecipeDoc)
from .render import render_html

A = Amount


def doc() -> RecipeDoc:
    ingredients = [
        Ingredient(id="ing_clams", name="fresh vongole (small clams)",
                   amount=A(imperial="2 lb", metric="1 kg")),
        Ingredient(id="ing_cornmeal", name="cornmeal",
                   amount=A(imperial="1 tbsp", metric="15 ml")),
        Ingredient(id="ing_spaghetti", name="spaghetti",
                   amount=A(imperial="1 lb", metric="450 g")),
        Ingredient(id="ing_oil", name="extra-virgin olive oil",
                   amount=A(imperial="¼ cup", metric="60 ml")),
        Ingredient(id="ing_garlic", name="garlic, cut into slivers",
                   amount=A.same("3 cloves")),
        Ingredient(id="ing_pepperoncini", name="small dried Italian peperoncini",
                   amount=A.same("2–4")),
        Ingredient(id="ing_salt", name="kosher salt & freshly ground black pepper",
                   amount=A.same("to taste")),
        Ingredient(id="ing_parsley", name="flat-leaf parsley, chopped (+ more to serve)",
                   amount=A.same("1 handful")),
        Ingredient(id="ing_water", name="cold water", amount=A.same("a bowl of")),
    ]

    equipment = [
        Equipment(id="eq_bowl", name="Large mixing bowl", size_matters=True,
                  size=A(imperial="5 qt+", metric="5 L+"), category="bowl",
                  why="Wide and deep enough to hold 2 lb of clams under plenty of cold water."),
        Equipment(id="eq_colander", name="Large colander", size_matters=True,
                  size=A(imperial="5 qt+", metric="5 L+"), category="colander",
                  why="Drains the soaked clams and, later, a full pound of spaghetti."),
        Equipment(id="eq_pot", name="Large pot", size_matters=True,
                  size=A(imperial="6–8 qt", metric="6–8 L"), category="pot",
                  why="Tall and roomy for a fast rolling boil."),
        Equipment(id="eq_tongs", name="Tongs or a pasta fork", size_matters=False,
                  category="tongs", why="For lifting and tossing the spaghetti."),
        Equipment(id="eq_pan", name="Heavy-bottomed sauté pan with lid", size_matters=True,
                  size=A(imperial="12 in", metric="30 cm"), category="pan",
                  why="Wide enough to hold every clam in a single layer, with a lid to steam them open."),
        Equipment(id="eq_spoon", name="Wooden spoon", size_matters=False,
                  category="spoon", why="For coaxing the garlic without scratching the pan."),
        Equipment(id="eq_knife", name="Chef's knife & cutting board", size_matters=False,
                  category="knife", why="For slivering garlic and chopping parsley."),
        Equipment(id="eq_timer", name="Timer", size_matters=False,
                  category="timer", why="The clams open in about 3 minutes."),
        Equipment(id="eq_serving", name="Serving bowls or a platter", size_matters=True,
                  size=A.same("for 4"), category="serving",
                  why="Warmed, so the pasta stays hot to the table. Serves 4."),
    ]

    steps = [
        Step(number=1, name="Clean & soak the clams",
             instruction=("Clean the clams, discarding any with broken shells or any that "
                          "won't close when you tap them. Soak the {ing0} in {ing1} with "
                          "{ing2} for about half an hour, then drain in the colander and "
                          "rinse to wash away grit and sand."),
             ingredients=[
                 StepIngredient(ingredient_id="ing_clams", amount=A(imperial="2 lb", metric="1 kg"), label="clams"),
                 StepIngredient(ingredient_id="ing_water", amount=A.same("a bowl of"), label="cold water"),
                 StepIngredient(ingredient_id="ing_cornmeal", amount=A(imperial="1 tbsp", metric="15 ml"), label="cornmeal"),
             ],
             equipment=[StepEquipment(equipment_id="eq_bowl"), StepEquipment(equipment_id="eq_colander")]),
        Step(number=2, name="Boil the spaghetti",
             instruction=("Bring the pot of water to a boil with {ing0}, then cook the {ing1} "
                          "according to the package instructions. Time it so the pasta finishes "
                          "right as the clams open."),
             ingredients=[
                 StepIngredient(ingredient_id="ing_salt", amount=A.same("a good amount of"), label="kosher salt"),
                 StepIngredient(ingredient_id="ing_spaghetti", amount=A(imperial="1 lb", metric="450 g"), label="spaghetti"),
             ],
             equipment=[StepEquipment(equipment_id="eq_pot"), StepEquipment(equipment_id="eq_tongs")]),
        Step(number=3, name="Start the garlic & chili oil",
             instruction=("Meanwhile, in the sauté pan, heat {ing0} over low heat. Add {ing1} "
                          "and {ing2}, and cook gently until the garlic is fragrant but not colored."),
             ingredients=[
                 StepIngredient(ingredient_id="ing_oil", amount=A(imperial="¼ cup", metric="60 ml"), label="extra-virgin olive oil"),
                 StepIngredient(ingredient_id="ing_garlic", amount=A.same("3 cloves"), label="garlic, slivered"),
                 StepIngredient(ingredient_id="ing_pepperoncini", amount=A.same("2–4"), label="dried peperoncini"),
             ],
             equipment=[StepEquipment(equipment_id="eq_pan"), StepEquipment(equipment_id="eq_spoon")]),
        Step(number=4, name="Add & season the clams",
             instruction=("Raise the heat to medium and add the {ing0}, shaking the pan and "
                          "stirring to coat them in the oil and garlic. Season with {ing1}, then "
                          "add {ing2} and toss to coat the clams once more."),
             ingredients=[
                 StepIngredient(ingredient_id="ing_clams", amount=A.same(""), label="clams", reused_from_step=1),
                 StepIngredient(ingredient_id="ing_salt", amount=A.same("a good amount of"), label="kosher salt and black pepper"),
                 StepIngredient(ingredient_id="ing_parsley", amount=A.same("1 handful"), label="chopped parsley"),
             ],
             equipment=[StepEquipment(equipment_id="eq_pan", reused_from_step=3),
                        StepEquipment(equipment_id="eq_knife"),
                        StepEquipment(equipment_id="eq_tongs", reused_from_step=2)]),
        Step(number=5, name="Cover & steam open",
             instruction=("Cover the pan and cook, shaking it every so often, until the clams "
                          "have opened and cooked through, {amt:about 3 minutes}. Discard any "
                          "that refuse to open."),
             ingredients=[],
             equipment=[StepEquipment(equipment_id="eq_pan", reused_from_step=3),
                        StepEquipment(equipment_id="eq_timer")]),
        Step(number=6, name="Combine & serve",
             instruction=("When the pasta and clams are both done, drain the spaghetti, add it to "
                          "the pan with the clams, and toss it through. Garnish with {ing0} and "
                          "serve at once."),
             ingredients=[
                 StepIngredient(ingredient_id="ing_parsley", amount=A.same("a little extra"), label="chopped parsley"),
             ],
             equipment=[StepEquipment(equipment_id="eq_colander", reused_from_step=1),
                        StepEquipment(equipment_id="eq_tongs", reused_from_step=2),
                        StepEquipment(equipment_id="eq_serving")]),
    ]

    return RecipeDoc(
        name="Spaghetti", subtitle="alle Vongole",
        author="Stanley Tucci · The Tucci Table",
        source_url="https://www.williams-sonoma.com/recipe/stanley-tucci-spaghetti-with-clams.html",
        recipe_yield=4, total_time_minutes=30,
        ingredients=ingredients, steps=steps, equipment=equipment,
    )


if __name__ == "__main__":
    print(render_html(doc()))
