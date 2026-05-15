# Pipeline constants. Ported from pipelineRecipes/pipeline/config.py so this
# project is the canonical source. The batch project should eventually import
# from here rather than maintain its own copy.

RECIPE_PHRASES = [
    "ingredients", "teaspoon", "teaspoons", "tsp", "tablespoon", "tablespoons", "tbsp",
    " cup", " cups", "1/4 cup", "1/3 cup", "1/2 cup", "2/3 cup", "3/4 cup",
    " ounce", " ounces", " oz", "-oz ", " lb", " pounds ", " kilos ", " gram", " grams", " g ",
    " ml", " litre", " litres", "pinch of", "sea salt", "kosher salt", "ground black pepper",
    "freshly ground", "clove", "allspice", "ground cumin", "ground cinnamon", "cinnamon stick",
    "coriander", "ground ginger", "cardamon", "cayenne", "paprika", "bay leaves", "bay leaf",
    "oregano", "turmeric", "fenugreek", "parsley", "capers", "cloves of", "to taste",
    "unsalted butter", "1 medium", "1 large", "medium onion", "large onion", "small onion",
    "red onion", "beef stock", "chicken stock", "chicken broth", "fish stock", "seafood stock",
    "ghee", "hard-boil", "boiling salted water", "bring to a boil", "diced ", "sprig ",
    "finely chopped ", "in a food processor", "chopped ", "minced ", "grated ", "peeled ",
    "gutted ", "crushed ", "finely crushed ", "steam ", "boil ", "add the", "add remaining",
    "boil the", "chop the", "cook the", "crack the", "cut into", "dice the", "discard the",
    "finely chop ", "coarsely chop", "grate the", "peel the", "not peeled", "prepare the",
    "juice the", "juice of", "pulse the", "mix the", "remove the", "rinse the", "rinse off",
    "salt the", "simmer", "low heat", "medium heat", "high heat", "spread the", "stir the",
    "stir in", "stir into", "fill the", "fill a", "set aside", "sprinkle the", "pour the",
    "scrub the", "seal the", "drizzle the", "spread the", "slide the", "fold in", "fold into",
    "bake for", "hand mixer", "stand mixer", "cool completely", "beat together", "beat until",
    "before serving", "serve immediately", "top with", "pour batter", "pour into", "pour over",
    "spread batter", "mash the", "mash a", "season to taste", "bring to a ", "serve as a ",
    "mince the", "whiz the", "bake the", "cool the", "preheat the", "knead the", "knead",
    "prick the", "spoon the", "chill for", "stuff the", "line the", "parchment", "skim the",
    "strain the", "strain sauce", "for serving", "tear up", "sauté ",
]

DISALLOWED_DOMAINS = {
    "youtube.com", "facebook.com", "reddit.com", "twitter.com",
    "pinterest.com", "tiktok.com", "linkedin.com",
}

IS_RECIPE_THRESHOLD = 7
MOZ_PASSING_GRADE = 0