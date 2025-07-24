import sqlite3
import json
import sys
from jinja2 import Template
from recipe_model import RecipeModel  # <- corrected import

DB_PATH = "recipes.db"
OUTPUT_HTML = "output_recipe_page.html"

def render_recipe_page(recipe_id: int):
    # Load recipe JSON from database
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT data FROM recipes WHERE id = ?", (recipe_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        raise ValueError(f"No recipe found with id = {recipe_id}")

    raw_json = json.loads(row[0])
    recipe = RecipeModel.parse_obj(raw_json)

    # Extract fields safely
    title = recipe.name or "Untitled Recipe"
    description = recipe.description or ""
    image_path = recipe.image[0] if recipe.image else ""
    ingredients = recipe.recipeIngredient or []
    instructions = recipe.recipeInstructions or []

    # Jinja2 HTML template
    template_str = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>{{ title }}</title>
        <style>
            body {
                font-family: Georgia, serif;
                max-width: 800px;
                margin: auto;
                padding: 2rem;
                background: #fff;
                color: #333;
            }
            img {
                width: 100%;
                border-radius: 10px;
                margin-bottom: 1rem;
            }
            h1 {
                font-size: 2em;
                margin-bottom: 0.5rem;
            }
            .description {
                font-style: italic;
                color: #555;
                margin-bottom: 1.5rem;
            }
            h2 {
                margin-top: 2rem;
                border-bottom: 1px solid #eee;
            }
            ul, ol {
                padding-left: 1.5rem;
            }
        </style>
    </head>
    <body>
        {% if image_path %}
        <img src="{{ image_path }}" alt="Recipe image">
        {% endif %}
        <h1>{{ title }}</h1>
        <div class="description">{{ description }}</div>

        <h2>Ingredients</h2>
        <ul>
            {% for item in ingredients %}
            <li>{{ item }}</li>
            {% endfor %}
        </ul>

        <h2>Instructions</h2>
        <ol>
            {% for step in instructions %}
            <li>{{ step.text if step and step.text else step }}</li>
            {% endfor %}
        </ol>
    </body>
    </html>
    """

    html = Template(template_str).render(
        title=title,
        description=description,
        image_path=image_path,
        ingredients=ingredients,
        instructions=instructions,
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ HTML recipe page created: {OUTPUT_HTML}")

# ---- CLI Entry ----
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python render_recipe_from_db.py <recipe_id>")
        sys.exit(1)

    try:
        recipe_id = int(sys.argv[1])
    except ValueError:
        print("Error: recipe_id must be an integer.")
        sys.exit(1)

    render_recipe_page(recipe_id)
