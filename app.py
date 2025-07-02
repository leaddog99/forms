from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import sqlite3
import json
from pathlib import Path

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

DATABASE = str(BASE_DIR / "recipes.db")

def get_recipe_by_id(recipe_id: int):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT data FROM recipes WHERE id = ?", (recipe_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        # Parse the JSON string into a dict
        return json.loads(row["data"])
    return None

@app.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def show_recipe(request: Request, recipe_id: int):
    recipe = get_recipe_by_id(recipe_id)
    if not recipe:
        return HTMLResponse("Recipe not found", status_code=404)
    return templates.TemplateResponse(
        "recipe.html",
        {"request": request, "recipe": recipe}
    )
