"""
build_ka_data.py
================
Transforms King Arthur Baking Company's published Ingredient Weight Chart into
a normalized JSON the conversion engine can load.

Source: King Arthur Baking Company, "Ingredient Weight Chart"
        https://www.kingarthurbaking.com/learn/ingredient-weight-chart
        (retrieved 2026-05-31)

KA publishes each ingredient at whatever serving volume is convenient
(1 cup, 1/2 cup, 1 tablespoon, etc.). We normalize every dry/liquid row to
grams-per-US-cup so the engine can derive a single density (g/mL). Count-based
rows (eggs, a head of garlic) are emitted separately as grams-per-item.

Only the underlying factual numbers are reproduced; KA is cited as the source.
"""

import json
from datetime import date
from fractions import Fraction

CUP_ML = 236.5882365
TBSP_PER_CUP = 16
TSP_PER_CUP = 48

# (name, volume_string, grams)  grams may be int or "LOW to HIGH"
ROWS = [
    ("'00' Pizza Flour", "1 cup", 116),
    ("Agave syrup", "1/4 cup", 84),
    ("All-Purpose Baking Mix", "1 cup", 120),
    ("All-Purpose Flour", "1 cup", 120),
    ("Almond butter", "1/4 cup", 68),
    ("Almond Flour", "1 cup", 96),
    ("Almond meal", "1 cup", 84),
    ("Almond paste (packed)", "1 cup", 259),
    ("Almonds (sliced)", "1/2 cup", 43),
    ("Almonds (slivered)", "1/2 cup", 57),
    ("Almonds, whole (unblanched)", "1 cup", 142),
    ("Amaranth flour", "1 cup", 103),
    ("Apple juice concentrate", "1/4 cup", 70),
    ("Apples (dried, diced)", "1 cup", 85),
    ("Apples (peeled, sliced)", "1 cup", 113),
    ("Applesauce", "1 cup", 255),
    ("Apricots (dried, diced)", "1/2 cup", 64),
    ("Artisan Bread Flour", "1 cup", 120),
    ("Artisan Bread Topping", "1/4 cup", 43),
    ("Baker's Cinnamon Filling", "1 cup", 152),
    ("Baker's Fruit Blend", "1 cup", 128),
    ("Baker's Special Sugar (superfine, castor)", "1 cup", 190),
    ("Baking powder", "1 teaspoon", 4),
    ("Baking soda", "1/2 teaspoon", 3),
    ("Baking Sugar Alternative", "1 cup", 170),
    ("Bananas (mashed)", "1 cup", 227),
    ("Barley (cooked)", "1 cup", 215),
    ("Barley (pearled)", "1 cup", 213),
    ("Barley flakes", "1/2 cup", 46),
    ("Barley flour", "1 cup", 85),
    ("Barley malt syrup", "2 tablespoons", 42),
    ("Basil pesto", "2 tablespoons", 28),
    ("Bell peppers (fresh)", "1 cup", 142),
    ("Berries (frozen)", "1 cup", 142),
    ("Better Cheddar Cheese Powder", "1/2 cup", 57),
    ("Blueberries (dried)", "1 cup", 156),
    ("Blueberries (fresh or frozen)", "1 cup", "140 to 170"),
    ("Blueberry juice", "1 cup", 241),
    ("Boiled cider", "1/4 cup", 85),
    ("Bran cereal", "1 cup", 60),
    ("Bread Flour", "1 cup", 120),
    ("Breadcrumbs (dried)", "1/4 cup", 28),
    ("Breadcrumbs (fresh)", "1/4 cup", 21),
    ("Breadcrumbs (Japanese Panko)", "1 cup", 50),
    ("Brown rice (cooked)", "1 cup", 170),
    ("Brown rice flour", "1 cup", 128),
    ("Brown sugar (dark or light, packed)", "1 cup", 213),
    ("Buckwheat (whole)", "1 cup", 170),
    ("Buckwheat Flour", "1 cup", 120),
    ("Bulgur", "1 cup", 152),
    ("Butter", "8 tablespoons", 113),
    ("Buttermilk", "1 cup", 227),
    ("Buttermilk Biscuit Flour Blend", "1 cup", 110),
    ("Buttermilk powder", "2 tablespoons", 18),
    ("Cacao nibs", "1 cup", 120),
    ("Cake Enhancer", "2 tablespoons", 14),
    ("Candied Lemon Peel", "1/4 cup", 37),
    ("Candied Orange Peel", "1/4 cup", 25),
    ("Caramel (1-inch squares)", "1/2 cup", 142),
    ("Caramel bits (chopped Heath or toffee)", "1 cup", 156),
    ("Caraway seeds", "2 tablespoons", 18),
    ("Carrots (cooked and pureed)", "1/2 cup", 128),
    ("Carrots (diced)", "1 cup", 142),
    ("Carrots (grated)", "1 cup", 99),
    ("Cashews (chopped)", "1 cup", 113),
    ("Cashews (whole)", "1 cup", 113),
    ("Celery (diced)", "1 cup", 142),
    ("Cheese (Feta)", "1/2 cup", 57),
    ("Cheese (grated cheddar, jack, mozzarella, or Swiss)", "1 cup", 113),
    ("Cheese (grated Parmesan)", "1/2 cup", 50),
    ("Cheese (Ricotta)", "1 cup", 227),
    ("Cherries (candied)", "1/4 cup", 50),
    ("Cherries (dried)", "1/2 cup", 71),
    ("Cherries (fresh, pitted, chopped)", "1/2 cup", 80),
    ("Cherries (frozen)", "1 cup", 113),
    ("Cherry Concentrate", "2 tablespoons", 42),
    ("Chia seeds", "1/4 cup", 37),
    ("Chickpea flour", "1 cup", 85),
    ("Chives (fresh)", "1/2 cup", 21),
    ("Chocolate (chopped)", "1 cup", 170),
    ("Chocolate Chips", "1 cup", 170),
    ("Chocolate Chunks", "1 cup", 170),
    ("Chocolate Mousse Mix", "1/4 cup", 28),
    ("Cinnamon Sweet Bits", "1/4 cup", 35),
    ("Cinnamon-Sugar", "1/4 cup", 50),
    ("Climate Blend Flour", "1 cup", 115),
    ("Cocoa (unsweetened)", "1/2 cup", 42),
    ("Coconut (sweetened, shredded)", "1 cup", 85),
    ("Coconut (toasted)", "1 cup", 85),
    ("Coconut (unsweetened, desiccated)", "1 cup", 85),
    ("Coconut (unsweetened, large flakes)", "1 cup", 60),
    ("Coconut (unsweetened, shredded)", "1 cup", 53),
    ("Coconut cream (unsweetened)", "1 cup", 284),
    ("Coconut Flour", "1 cup", 128),
    ("Coconut milk (evaporated)", "1 cup", 242),
    ("Coconut Milk Powder", "1/2 cup", 57),
    ("Coconut milk (canned, well shaken)", "1 cup", 241),
    ("Coconut oil", "1/2 cup", 113),
    ("Coconut sugar", "1/2 cup", 77),
    ("Confectioners' sugar (unsifted)", "1 cup", 113),
    ("Cookie butter", "1/4 cup", 72),
    ("Cookie crumbs", "1 cup", 85),
    ("Corn (fresh or frozen)", "1/4 cup", 38),
    ("Corn (popped)", "4 cups", 21),
    ("Corn syrup", "1 cup", 312),
    ("Cornmeal (whole)", "1 cup", 138),
    ("Cornmeal (yellow, Quaker)", "1 cup", 156),
    ("Cornstarch", "1/4 cup", 28),
    ("Cottage cheese", "1/2 cup", 113),
    ("Cracked wheat", "1 cup", 149),
    ("Cranberries (dried)", "1/2 cup", 57),
    ("Cranberries (fresh or frozen)", "1 cup", 99),
    ("Cream (heavy, light, or half & half)", "1 cup", 227),
    ("Cream cheese", "1 cup", 227),
    ("Cream of coconut", "1/2 cup", 142),
    ("Creme fraiche", "1/2 cup", 113),
    ("Currants", "1 cup", 142),
    ("Dates (chopped)", "1 cup", 149),
    ("Demerara sugar", "1 cup", 220),
    ("Dried Blueberry Powder", "1/4 cup", 28),
    ("Dried milk (Baker's Special Dry Milk)", "1/4 cup", 28),
    ("Dried nonfat milk (powdered)", "1/4 cup", 28),
    ("Dried potato flakes (instant mashed potatoes)", "1/2 cup", 43),
    ("Dried whole milk (powdered)", "1/2 cup", 50),
    ("Durum Flour", "1 cup", 124),
    ("Easy Roll Dough Improver", "2 tablespoons", 18),
    ("Egg whites (dried)", "2 tablespoons", 11),
    ("Espresso Powder", "1 tablespoon", 7),
    ("Everything Bagel Topping", "1/4 cup", 35),
    ("Figs (dried, chopped)", "1 cup", 149),
    ("First Clear Flour", "1 cup", 106),
    ("Flax meal", "1/2 cup", 50),
    ("Flaxseed", "1/4 cup", 35),
    ("Formaggio Italiano Cheese and Herb Blend", "1/4 cup", 30),
    ("French-Style Flour", "1 cup", 120),
    ("Fruitcake Fruit Blend", "1 cup", 120),
    ("Garlic (minced)", "2 tablespoons", 28),
    ("Garlic (peeled and sliced)", "1 cup", 149),
    ("Ghee", "1/4 cup", 44),
    ("Ginger (fresh, sliced)", "1/4 cup", 57),
    ("Gluten-Free All-Purpose Baking Mix", "1 cup", 120),
    ("Gluten-Free All-Purpose Flour", "1 cup", 156),
    ("Gluten-Free Bread Flour", "1 cup", 120),
    ("Gluten-Free Measure for Measure Flour", "1 cup", 120),
    ("Gluten-Free Pizza Flour", "1 cup", 100),
    ("Glutinous rice flour", "1 cup", 120),
    ("Golden Wheat Flour", "1 cup", 113),
    ("Graham cracker crumbs", "1 cup", 100),
    ("Granola", "1 cup", 113),
    ("Grape Nuts", "1/2 cup", 57),
    ("Guava paste", "1/4 cup", 100),
    ("Harvest Grains Blend", "1/2 cup", 74),
    ("Hazelnut flour", "1 cup", 89),
    ("Hazelnut Praline Paste", "1/2 cup", 156),
    ("Hazelnut spread", "1/2 cup", 160),
    ("Hazelnuts (whole)", "1 cup", 142),
    ("Hi-Maize Natural Fiber", "1/4 cup", 32),
    ("High-Gluten Flour", "1 cup", 120),
    ("Honey", "1 tablespoon", 21),
    ("Instant ClearJel", "1 tablespoon", 11),
    ("Irish-Style Flour", "1 cup", 110),
    ("Italian Herb Seasoning", "2 tablespoons", 14),
    ("Italian-Style Flour", "1 cup", 106),
    ("Jam or preserves", "1/4 cup", 85),
    ("Jammy Bits", "1 cup", 184),
    ("Keto Wheat Flour", "1 cup", 120),
    ("Keto Wheat Pizza Crust Mix", "1 cup", 110),
    ("Key Lime Juice", "1 cup", 227),
    ("Lard", "1/2 cup", 113),
    ("Leeks (diced)", "1 cup", 92),
    ("Lemon Crumbles", "1 cup", 180),
    ("Lemon Curd", "1/2 cup", 113),
    ("Lemon juice", "1 tablespoon", 14),
    ("Lemon Juice Powder", "2 tablespoons", 18),
    ("Lime Juice Powder", "2 tablespoons", 18),
    ("Macadamia nuts (whole)", "1 cup", 149),
    ("Malt syrup", "2 tablespoons", 43),
    ("Malted Milk Powder", "1/4 cup", 35),
    ("Malted Wheat Flakes", "1/2 cup", 64),
    ("Maple Cinnamon French Toast Sugar", "1/4 cup", 48),
    ("Maple Cream", "2 tablespoons", 42),
    ("Maple sugar", "1/2 cup", 78),
    ("Maple syrup", "1/2 cup", 156),
    ("Marshmallow Fluff", "1 cup", 128),
    ("Marshmallow spread (homemade)", "1 cup", 72),
    ("Marshmallow spread (store-bought)", "1 cup", 123),
    ("Marshmallows (mini)", "1 cup", 43),
    ("Marzipan", "1 cup", 290),
    ("Masa Harina", "1 cup", 93),
    ("Mascarpone cheese", "1 cup", 227),
    ("Mashed potatoes", "1 cup", 213),
    ("Mashed sweet potatoes", "1 cup", 240),
    ("Matcha powder", "2 tablespoons", 12),
    ("Mayonnaise", "1/2 cup", 113),
    ("Medium Rye Flour", "1 cup", 106),
    ("Meringue powder", "1/4 cup", 43),
    ("Milk (evaporated)", "1/2 cup", 113),
    ("Milk (fresh)", "1 cup", 227),
    ("Millet (whole)", "1/2 cup", 103),
    ("Mini chocolate chips", "1 cup", 177),
    ("Mini Diced Ginger", "1/2 cup", 73),
    ("Molasses", "1/4 cup", 85),
    ("Mushrooms (sliced)", "1 cup", 78),
    ("Non-Diastatic Malt Powder", "2 tablespoons", 18),
    ("Nutella", "1/2 cup", 149),
    ("Oat bran", "1/2 cup", 53),
    ("Oat Flour", "1 cup", 92),
    ("Oats (rolled)", "1 cup", 113),
    ("Oats (old-fashioned or quick-cooking)", "1 cup", 89),
    ("Oats (prepared)", "1 cup", 147),
    ("Olive oil", "1/4 cup", 50),
    ("Olives (sliced)", "1 cup", 142),
    ("Onions (fresh, diced)", "1 cup", 142),
    ("Paleo Baking Flour", "1 cup", 104),
    ("Palm shortening", "1/4 cup", 45),
    ("Passion fruit puree", "1/3 cup", 60),
    ("Pasta Flour Blend", "1 cup", 145),
    ("Pastry Flour", "1 cup", 106),
    ("Pastry Flour Blend", "1 cup", 113),
    ("Peaches (peeled and diced)", "1 cup", 170),
    ("Peanut butter", "1/2 cup", 135),
    ("Peanuts (whole, shelled)", "1 cup", 142),
    ("Pears (peeled and diced)", "1 cup", 163),
    ("Pecan Meal", "1 cup", 80),
    ("Pecans (diced)", "1/2 cup", 57),
    ("Pecans (whole)", "1 cup", 105),
    ("Pie Filling Enhancer", "1/4 cup", 46),
    ("Pine nuts", "1/2 cup", 71),
    ("Pineapple (crushed, drained)", "1 cup", 256),
    ("Pineapple (dried)", "1/2 cup", 71),
    ("Pineapple (fresh or canned, diced)", "1 cup", 170),
    ("Pistachio nuts (shelled)", "1/2 cup", 60),
    ("Pistachio Paste", "1/4 cup", 78),
    ("Pizza Dough Flavor", "2 tablespoons", 12),
    ("Pizza Flour Blend", "1 cup", 124),
    ("Pizza sauce", "1/4 cup", 57),
    ("Pizza Seasoning", "2 tablespoons", 10),
    ("Polenta (coarse ground cornmeal)", "1 cup", 163),
    ("Poppy seeds", "2 tablespoons", 18),
    ("Potato Flour", "1/4 cup", 46),
    ("Potato starch", "1 cup", 152),
    ("Pumpernickel Flour", "1 cup", 106),
    ("Pumpkin puree", "1 cup", 227),
    ("Pumpkin seeds", "1/4 cup", 40),
    ("Queso fresco", "1/2 cup", 57),
    ("Quinoa (cooked)", "1 cup", 184),
    ("Quinoa (whole)", "1 cup", 177),
    ("Quinoa flour", "1 cup", 110),
    ("Raisins (loose)", "1 cup", 149),
    ("Raisins (packed)", "1/2 cup", 85),
    ("Raspberries (fresh)", "1 cup", 120),
    ("Rhubarb (sliced)", "1 cup", "120 to 140"),
    ("Rice (long grain, dry)", "1/2 cup", 99),
    ("Rice flour (white)", "1 cup", 142),
    ("Rice Krispies", "1 cup", 28),
    ("Rye Bread Improver", "2 tablespoons", 14),
    ("Rye Chops", "1 cup", 120),
    ("Rye flakes", "1 cup", 124),
    ("Rye Flour Blend", "1 cup", 106),
    ("Salt (Kosher, Diamond Crystal)", "1 tablespoon", 8),
    ("Salt (Kosher, Morton's)", "1 tablespoon", 16),
    ("Salt (table)", "1 tablespoon", 18),
    ("Scallions (sliced)", "1 cup", 64),
    ("Self-Rising Flour", "1 cup", 113),
    ("Semolina Flour", "1 cup", 163),
    ("Sesame seeds", "1/2 cup", 71),
    ("Shallots (peeled and sliced)", "1 cup", 156),
    ("Six-Grain Blend", "1 cup", 128),
    ("Snow White Non-Melting Topping Sugar", "1/2 cup", 57),
    ("Sorghum flour", "1 cup", 138),
    ("Sour cream", "1 cup", 227),
    ("Sourdough starter", "1 cup", "227 to 241"),
    ("Soy flour", "1/4 cup", 35),
    ("Sparkling Sugar", "1/4 cup", 57),
    ("Spelt Flour", "1 cup", 99),
    ("Sprouted Wheat Flour", "1 cup", 113),
    ("Steel cut oats", "1/2 cup", 70),
    ("Sticky Bun Sugar", "1 cup", 99),
    ("Strawberries (fresh, sliced)", "1 cup", 167),
    ("Sugar (granulated white)", "1 cup", 198),
    ("Sugar substitute (Splenda)", "1 cup", 25),
    ("Sundried tomatoes (dry pack)", "1 cup", 170),
    ("Sunflower seeds", "1/4 cup", 35),
    ("Super 10 Blend", "1 cup", 106),
    ("Swedish Pearl Sugar", "1/4 cup", 49),
    ("Sweetened condensed coconut milk", "1 cup", 288),
    ("Sweetened condensed milk", "1/4 cup", 78),
    ("Tahini paste", "1/2 cup", 128),
    ("Tapioca (quick cooking)", "2 tablespoons", 21),
    ("Tapioca starch or flour", "1 cup", 113),
    ("Teff flour", "1 cup", 135),
    ("The Works Bread Topping", "1/4 cup", 35),
    ("Toasted Almond Flour", "1 cup", 96),
    ("Toffee chunks", "1 cup", 156),
    ("Tomato paste", "2 tablespoons", 29),
    ("Tropical Fruit Blend", "1 cup", "128 to 142"),
    ("Turbinado sugar (raw)", "1 cup", 180),
    ("Unbleached Cake Flour", "1 cup", 120),
    ("Vanilla Extract", "1 tablespoon", 14),
    ("Vegetable oil", "1 cup", 198),
    ("Vegetable shortening", "1/4 cup", 46),
    ("Vital Wheat Gluten", "2 tablespoons", 18),
    ("Walnuts (chopped)", "1 cup", 113),
    ("Walnuts (whole)", "1/2 cup", 64),
    ("Water", "1 cup", 227),
    ("Wheat berries (red)", "1 cup", 184),
    ("Wheat bran", "1/2 cup", 32),
    ("Wheat germ", "1/4 cup", 28),
    ("White Chocolate Chips", "1 cup", 170),
    ("White Rye Flour", "1 cup", 106),
    ("Whole Grain Flour Blend", "1 cup", 113),
    ("Whole Wheat Flour (Premium 100%)", "1 cup", 113),
    ("Whole Wheat Pastry Flour / Graham Flour", "1 cup", 96),
    ("Yeast (instant)", "1 tablespoon", 9),
    ("Yogurt", "1 cup", 227),
    ("Yuletide Cheer Fruit Blend", "1 cup", 130),
    ("Zucchini (shredded)", "1 cup", "121 to 150"),
]

# Count-based items (grams per single item). KA values where given;
# garlic clove is a documented estimate (KA lists only a whole head).
COUNT_ITEMS = [
    ("egg", 50, ["eggs", "large egg", "egg (fresh)"], "King Arthur"),
    ("egg white", 33, ["egg whites"], "King Arthur (30-35 g, midpoint)"),
    ("egg yolk", 14, ["egg yolks"], "King Arthur"),
    ("garlic head", 113, ["head of garlic"], "King Arthur (cloves in skin)"),
    ("garlic clove", 3, ["clove", "garlic clove"], "estimate (not in KA chart)"),
]

# Curated short-name aliases mapping to the KA canonical names above.
ALIASES = {
    "all-purpose flour": ["flour", "ap flour", "plain flour", "all purpose flour"],
    "bread flour": ["bread"],
    "sugar (granulated white)": ["sugar", "granulated sugar", "white sugar",
                                  "caster sugar", "castor sugar"],
    "brown sugar (dark or light, packed)": ["brown sugar", "light brown sugar",
                                             "dark brown sugar"],
    "confectioners' sugar (unsifted)": ["powdered sugar", "icing sugar",
                                         "confectioners sugar"],
    "butter": ["unsalted butter", "salted butter"],
    "milk (fresh)": ["milk", "whole milk"],
    "water": ["h2o"],
    "honey": [],
    "vegetable oil": ["oil", "canola oil"],
    "olive oil": ["evoo"],
    "cocoa (unsweetened)": ["cocoa", "cocoa powder", "unsweetened cocoa"],
    "chocolate chips": ["choc chips"],
    "oats (old-fashioned or quick-cooking)": ["oats", "rolled oats (generic)"],
    "salt (table)": ["salt", "table salt"],
    "cornstarch": ["corn starch"],
    "sour cream": [],
    "cream cheese": [],
}


def vol_to_cups(volume: str) -> float | None:
    """Return the volume in cups, or None if it's a count unit (large/head)."""
    v = volume.replace("\u00ad", "").strip().lower()
    # strip any parenthetical, e.g. "8 tablespoons (1/2 cup)"
    if "(" in v:
        v = v.split("(")[0].strip()
    parts = v.split()
    # find the unit word
    if "cup" in v:
        unit, per_cup = "cup", 1.0
    elif "tablespoon" in v:
        unit, per_cup = "tablespoon", TBSP_PER_CUP
    elif "teaspoon" in v:
        unit, per_cup = "teaspoon", TSP_PER_CUP
    else:
        return None  # "1 large", "1 large head", etc. -> count item

    # quantity is everything before the unit word
    qty_tokens = []
    for p in parts:
        if p.startswith(unit):
            break
        qty_tokens.append(p)
    qty = sum(Fraction(t) for t in qty_tokens) if qty_tokens else Fraction(1)
    return float(qty) / per_cup


def grams_value(g) -> float:
    if isinstance(g, str) and "to" in g:
        lo, hi = (float(x) for x in g.split("to"))
        return round((lo + hi) / 2)
    return float(g)


def main():
    ingredients = []
    skipped = []
    for name, volume, grams in ROWS:
        cups = vol_to_cups(volume)
        if cups is None:
            skipped.append((name, volume))
            continue
        g = grams_value(grams)
        gpc = round(g / cups, 2)
        ingredients.append({
            "name": name,
            "grams_per_cup": gpc,
            "g_per_ml": round(gpc / CUP_ML, 5),
            "source_volume": volume,
        })

    # attach aliases
    by_name = {i["name"].lower(): i for i in ingredients}
    for canon, alist in ALIASES.items():
        if canon.lower() in by_name:
            by_name[canon.lower()]["aliases"] = alist
        else:
            print(f"  ! alias target not found: {canon!r}")

    count_items = [{
        "name": n, "grams_per_item": g, "aliases": a, "source": src
    } for (n, g, a, src) in COUNT_ITEMS]

    data = {
        "source": ("King Arthur Baking Company, Ingredient Weight Chart, "
                   "https://www.kingarthurbaking.com/learn/ingredient-weight-chart"),
        "retrieved": str(date.today()),
        "note": ("Gram-per-cup values normalized from KA's published "
                 "volume/weight pairs. Factual data; KA cited as source. "
                 "KA rounds water/milk to 227 g/cup (8 oz convention); true "
                 "water is ~236 g/cup. Flour is the spoon-and-level method."),
        "cup_ml": CUP_ML,
        "ingredient_count": len(ingredients),
        "ingredients": sorted(ingredients, key=lambda x: x["name"].lower()),
        "count_items": count_items,
    }

    out = "/home/claude/kingarthur_ingredient_weights.json"
    with open(out, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(ingredients)} density ingredients + "
          f"{len(count_items)} count items -> {out}")
    if skipped:
        print(f"Routed to count/other ({len(skipped)}): "
              + ", ".join(n for n, _ in skipped))
    # spot checks
    for probe in ["all-purpose flour", "sugar (granulated white)",
                  "honey", "butter", "water"]:
        i = by_name[probe]
        print(f"  {probe:35s} {i['grams_per_cup']:6.1f} g/cup "
              f"({i['g_per_ml']:.3f} g/mL)  from {i['source_volume']}")


if __name__ == "__main__":
    main()
