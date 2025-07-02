import sqlite3
import json
from datetime import datetime

# The JSON recipe object (as a Python dict)
recipe_json = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "id": "1",
    "name": "French Toast",
    "description": "Classic French toast recipe with a custardy center. Perfect for breakfast or brunch.",
    "image": [
        "https://example.com/images/french-toast-1.jpg"
    ],
    "author": {
        "@type": "Person",
        "name": "Jane Doe",
        "image": "https://example.com/authors/jane.jpg"
    },
    "datePublished": "2025-07-01T13:00:00Z",
    "dateModified": "2025-07-01T13:00:00Z",
    "recipeYield": "4 servings",
    "prepTime": "PT10M",
    "cookTime": "PT15M",
    "totalTime": "PT25M",
    "recipeCategory": "Breakfast",
    "recipeCuisine": "American",
    "keywords": ["breakfast", "french toast", "easy"],
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": 4.8,
        "reviewCount": 256
    },
    "nutrition": {
        "@type": "NutritionInformation",
        "calories": "320 calories",
        "fatContent": "12g",
        "carbohydrateContent": "38g",
        "proteinContent": "10g"
    },
    "recipeIngredient": [
        "4 large eggs",
        "1 cup milk",
        "8 slices bread",
        "1 tsp vanilla extract",
        "1/2 tsp cinnamon",
        "Butter, for frying"
    ],
    "recipeInstructions": [
        {
            "@type": "HowToStep",
            "position": 1,
            "name": "Make the custard",
            "text": "Whisk together eggs, milk, vanilla, and cinnamon in a large bowl.",
            "image": "https://example.com/images/step1.jpg",
            "imageCredit": "Photo by Jane Doe"
        },
        {
            "@type": "HowToStep",
            "position": 2,
            "name": "Soak the bread",
            "text": "Dip bread slices into the custard, coating both sides.",
            "image": "https://example.com/images/step2.jpg",
            "imageCredit": "Photo by Jane Doe"
        },
        {
            "@type": "HowToStep",
            "position": 3,
            "name": "Cook the French toast",
            "text": "Fry soaked bread in butter over medium heat until golden brown on both sides.",
            "image": "https://example.com/images/step3.jpg",
            "imageCredit": "Photo by Jane Doe"
        }
    ],
    "notes": "For best results, use slightly stale bread.",
    "tags": ["vegetarian", "quick", "brunch"],
    "video": "https://example.com/videos/french-toast.mp4",
    "_imported_from": "Grandma's Cookbook",
    "_editor_version": "v2.1",
    "_access": {
        "visibility": "private",
        "sharedWith": ["friend1", "friend2"]
    },
    "_source": {
        "type": "image",
        "origin": "Grandma's handwritten recipe",
        "originalUrl": "https://simplyrecipes.com/recipes/french_toast/"
    }
}

# Connect to your SQLite database
conn = sqlite3.connect("recipes.db")
cur = conn.cursor()

# Prepare timestamps
now = datetime.utcnow().isoformat() + "Z"

# Insert the recipe
cur.execute(
    """
    INSERT INTO recipes (user_id, data, created_at, updated_at)
    VALUES (?, ?, ?, ?)
    """,
    (
        1,  # user_id (change as needed)
        json.dumps(recipe_json),  # serialize the dict to a JSON string
        now,
        now
    )
)

conn.commit()
conn.close()
