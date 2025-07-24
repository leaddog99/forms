from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional
import sqlite3
import uuid
import json
from datetime import datetime
from contextlib import asynccontextmanager


# Pydantic Model for Recipe
class RecipeModel(BaseModel):
    recipe_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    ingredients: List[str] = []
    instructions: List[str] = []
    sourceTitle: Optional[str] = None
    sourceOrigin: Optional[str] = None
    originalUrl: Optional[str] = None
    affiliateUrl: Optional[str] = None

    class Config:
        schema_extra = {
            "example": {
                "name": "Chocolate Chip Cookies",
                "description": "Classic homemade chocolate chip cookies",
                "ingredients": [
                    "2 1/4 cups all-purpose flour",
                    "1 tsp baking soda",
                    "1 cup butter, softened"
                ],
                "instructions": [
                    "Preheat oven to 375°F",
                    "Mix dry ingredients",
                    "Cream butter and sugar"
                ]
            }
        }


# Sanitization function
def sanitize_recipe_input(data: dict) -> dict:
    # Basic sanitization - remove any potentially harmful content
    sanitized = {
        k: v for k, v in data.items()
        if k in [
            'recipe_id', 'name', 'description', 'ingredients',
            'instructions', 'sourceTitle', 'sourceOrigin',
            'originalUrl', 'affiliateUrl'
        ]
    }

    # Ensure ingredients and instructions are lists
    if 'ingredients' in sanitized and isinstance(sanitized['ingredients'], str):
        sanitized['ingredients'] = [ing.strip() for ing in sanitized['ingredients'].split('\n') if ing.strip()]

    if 'instructions' in sanitized and isinstance(sanitized['instructions'], str):
        sanitized['instructions'] = [inst.strip() for inst in sanitized['instructions'].split('\n') if inst.strip()]

    return sanitized


# Database Path
DB_PATH = "recipe_database.db"


# Initialize Database
def create_recipe_table():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recipe_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id TEXT UNIQUE,
                user_id INTEGER DEFAULT 1,
                recipe_data TEXT,
                created_at TEXT,
                updated_at TEXT
            );
        """)


# Lifespan Context Manager
@asynccontextmanager
async def recipe_app_lifespan(app: FastAPI):
    create_recipe_table()
    yield


# Create FastAPI Application
app = FastAPI(
    title="Recipe Management API",
    description="API for storing and retrieving recipes",
    version="0.1.0",
    lifespan=recipe_app_lifespan
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Get All Recipes Endpoint
@app.get("/recipes",
         summary="List All Recipes",
         description="Retrieve all stored recipes from the database")
def list_all_recipes():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, recipe_id, recipe_data, created_at, updated_at FROM recipe_entries ORDER BY updated_at DESC")
            rows = cursor.fetchall()

            return [
                {
                    "id": row[0],
                    "recipe_id": row[1],
                    "data": json.loads(row[2]),
                    "created_at": row[3],
                    "updated_at": row[4]
                } for row in rows
            ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database retrieval error: {e}")


# Save Recipe Endpoint
@app.post("/recipes",
          summary="Save a Recipe",
          description="Create or update a recipe in the database")
def save_recipe(recipe: RecipeModel):
    # Generate a unique recipe ID if not provided
    recipe_id = recipe.recipe_id or str(uuid.uuid4())

    # Timestamp for creation/update
    now = datetime.utcnow().isoformat()

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO recipe_entries (recipe_id, recipe_data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(recipe_id) DO UPDATE SET
                    recipe_data = excluded.recipe_data,
                    updated_at = excluded.updated_at;
            """, (
                recipe_id,
                json.dumps(recipe.dict(), indent=2),
                now,
                now
            ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database save error: {e}")

    return {"recipe_id": recipe_id, "message": "Recipe saved successfully"}


# Health Check Endpoint
@app.get("/health", summary="Health Check", description="Check if the API is running")
def health_check():
    return {"status": "healthy", "message": "Recipe API is up and running!"}