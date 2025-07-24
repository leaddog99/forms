from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
import sqlite3
import uuid
import json
from datetime import datetime
import os
import traceback

# IMPORTANT: Keep the imports for the critical business logic files
try:
    from recipe_model import RecipeModel

    print("✅ RecipeModel imported successfully")
except Exception as e:
    print(f"❌ Failed to import RecipeModel: {e}")
    raise

try:
    from sanitize_recipe_data import sanitize_recipe_data

    print("✅ sanitize_recipe_data imported successfully")
except Exception as e:
    print(f"❌ Failed to import sanitize_recipe_data: {e}")
    raise

print("🚀 Starting API setup...")

DB_PATH = "recipes.db"


# Ensure table exists
def init_db():
    print("🔧 Creating database table if needed...")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipe_id TEXT UNIQUE,
                    user_id INTEGER,
                    data TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
            """)
        print("✅ Database table ready")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        raise


# Initialize the app without lifespan for now to avoid hanging
app = FastAPI()

# Initialize DB immediately instead of using lifespan
print("🔧 Initializing database...")
init_db()
print("✅ Database initialized successfully")

print("🌐 Setting up CORS...")

# CORS for frontend interaction
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

print("📁 Setting up static files...")

# Serve static HTML files (e.g., recipe_form.html)
try:
    forms_path = os.path.dirname(__file__)  # Use the directory this file is in
    app.mount("/forms", StaticFiles(directory=forms_path), name="forms")
    print("✅ Static files mounted successfully")
except Exception as e:
    print(f"⚠️ Static files mount failed: {e}")

print("📡 Setting up routes...")


# Health check
@app.get("/")
def health_check():
    print("❤️ Health check endpoint called")
    return {"status": "ok", "message": "Full API with error handling"}


# List all recipes
@app.get("/recipes")
def list_recipes():
    print("📋 List recipes endpoint called")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, recipe_id, data, created_at, updated_at FROM recipes ORDER BY updated_at DESC")
            rows = cursor.fetchall()
            result = []

            for row in rows:
                try:
                    recipe_entry = {
                        "id": row[0],
                        "recipe_id": row[1],
                        "data": json.loads(row[2]),
                        "created_at": row[3],
                        "updated_at": row[4]
                    }
                    result.append(recipe_entry)
                except json.JSONDecodeError as e:
                    print(f"⚠️ Failed to parse recipe {row[1]}: {e}")
                    continue

            print(f"✅ Returning {len(result)} recipes")
            return result

    except Exception as e:
        print(f"❌ Error in list_recipes: {e}")
        print(f"❌ Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


# Save (insert or update) a recipe
@app.post("/recipes")
async def save_recipe(request: Request):
    print("💾 Save recipe endpoint called")
    try:
        # Get the payload
        payload = await request.json()
        print(f"📝 Received payload: {payload}")

        # IMPORTANT: Use the critical business logic files
        cleaned = sanitize_recipe_data(payload)
        print(f"🧹 Sanitized data: {cleaned}")

        recipe = RecipeModel(**cleaned)
        print("✅ Recipe model validation passed")

    except ValidationError as e:
        print(f"❌ Validation error: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        print(f"❌ Error processing request: {e}")
        print(f"❌ Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=400, detail=f"Bad input: {e}")

    recipe_id = payload.get("recipe_id") or str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    user_id = 1  # Placeholder

    print(f"💾 Saving recipe with ID: {recipe_id}")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO recipes (recipe_id, user_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recipe_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at;
            """, (
                recipe_id,
                user_id,
                json.dumps(recipe.dict(), indent=2),
                now,
                now
            ))
            print("✅ Recipe saved to database")
    except Exception as e:
        print(f"❌ Database error: {e}")
        print(f"❌ Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    return {"recipe_id": recipe_id}


# Delete a recipe
@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: str):
    print(f"🗑️ Delete recipe endpoint called for: {recipe_id}")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM recipes WHERE recipe_id = ?", (recipe_id,))
            if cursor.rowcount == 0:
                print(f"❌ Recipe {recipe_id} not found")
                raise HTTPException(status_code=404, detail="Recipe not found")
            conn.commit()
            print(f"✅ Recipe {recipe_id} deleted successfully")
        return {"message": "Recipe deleted successfully"}
    except Exception as e:
        print(f"❌ Error deleting recipe: {e}")
        print(f"❌ Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


print("🎉 API setup complete!")