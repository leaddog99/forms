import sqlite3

recipe = {
    "breadcrumbs": "RECIPES > RECIPES BY TIME AND EASE > QUICK RECIPES",
    "title": "The Best French Toast",
    "subtitle": "Fluffy and tender inside, gloriously browned outside, this is the very best recipe for the brunch classic.",
    "author": "Elise Bauer",
    "author_image": "https://www.simplyrecipes.com/thmb/KN95Fj8CYtatUiS-XrT76y4XEjg=/40x40/filters:no_upscale():max_bytes(150000):strip_icc():format(webp)/SRHeadshots-EliseBauer-5c36c598a88d4ba3bff66260a792ea47.jpg",
    "created_date": "August 15, 2024",
    "star_rating": 5,
    "ratings_count": 46,
    "notes": ""
}

with sqlite3.connect('recipes.db') as conn:
    c = conn.cursor()
    c.execute("""
        INSERT INTO recipes
        (breadcrumbs, title, subtitle, author, author_image, created_date, star_rating, ratings_count, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        recipe["breadcrumbs"],
        recipe["title"],
        recipe["subtitle"],
        recipe["author"],
        recipe["author_image"],
        recipe["created_date"],
        recipe["star_rating"],
        recipe["ratings_count"],
        recipe["notes"]
    ))
    conn.commit()
