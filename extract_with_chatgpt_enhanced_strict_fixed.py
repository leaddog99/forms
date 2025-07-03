
import requests
import json
from openai import OpenAI
from pydantic import ValidationError
from recipe_model import RecipeModel  # assumes your schema is in recipe_model.py

client = OpenAI()

ENHANCED_PROMPT = """
You are a food historian and structured data expert. 
Given a recipe URL, your job is to return a fully populated JSON object using the schema provided below.

You must:
- Carefully extract recipe fields from the HTML content.
- If any field is missing, estimate it reasonably.
- Use culinary knowledge to enrich the `history` field with details like ethnicity, origin region, traditional context, notable variations, and related dishes.
- Treat `notableVariations` and `relatedDishes` as lists.
- Leave `firstDocumented` as null if unknown.
- Populate `sources` with historical references, cookbooks, or Amazon book links if applicable.
- Return only a valid JSON object in the structure below. Do NOT add commentary.

Schema:
{json_schema}

Use the following web page to extract and enhance the recipe:
{url}
"""

def extract_recipe_from_url_with_gpt(url: str):
    print(f"📥 Extracting from: {url}")

    # Inline schema definition passed to the prompt (shortened version to fit here)
    with open("recipe_schema_template.json", "r") as f:
        schema_text = f.read()

    prompt = ENHANCED_PROMPT.replace("{json_schema}", schema_text).replace("{url}", url)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )

    raw_json = response.choices[0].message.content.strip()

    try:
        parsed = json.loads(raw_json)
    except Exception as e:
        print("❌ Failed to parse JSON")
        print(raw_json)
        raise e

    try:
        recipe = RecipeModel.model_validate(parsed)
        print("✅ Successfully validated recipe")
        return recipe
    except ValidationError as ve:
        print("❌ Schema validation failed:")
        print(ve)
        with open("failed_recipe_raw_output.json", "w") as f:
            json.dump(parsed, f, indent=2)
        raise ve


# Example test
if __name__ == "__main__":
    test_url = "https://cooking.nytimes.com/recipes/1020478-greek-lemon-potatoes"
    recipe = extract_recipe_from_url_with_gpt(test_url)
    with open("validated_recipe.json", "w") as f:
        json.dump(recipe.model_dump(), f, indent=2)
