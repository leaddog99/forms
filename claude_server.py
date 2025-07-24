import logging
import uuid
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from typing import List, Optional

# Configure detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Outputs to console
        logging.FileHandler('recipe_server.log')  # Outputs to file for persistent logging
    ]
)
logger = logging.getLogger(__name__)


# Data model for recipes
class RecipeData(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=1000)


class RecipeResponse(BaseModel):
    recipe_id: str
    data: RecipeData


# In-memory storage (replace with database in production)
class RecipeStorage:
    def __init__(self):
        self._recipes = {}

    def get_all_recipes(self) -> List[RecipeResponse]:
        recipes = [
            RecipeResponse(recipe_id=recipe_id, data=recipe_data)
            for recipe_id, recipe_data in self._recipes.items()
        ]
        logger.info(f"Fetched {len(recipes)} recipes")
        return recipes

    def create_recipe(self, recipe: RecipeData) -> RecipeResponse:
        # Generate a unique ID for the recipe
        recipe_id = str(uuid.uuid4())

        # Store the recipe
        self._recipes[recipe_id] = recipe

        # Create and return the response
        response = RecipeResponse(recipe_id=recipe_id, data=recipe)

        logger.info(f"Created recipe: {recipe.name} (ID: {recipe_id})")
        return response


# Create FastAPI app
app = FastAPI(title="Recipe Management API")


# Add extensive logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.debug(f"Incoming request: {request.method} {request.url}")
    try:
        response = await call_next(request)
        logger.debug(f"Response status: {response.status_code}")
        return response
    except Exception as e:
        logger.error(f"Unhandled exception: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"message": "Internal server error"}
        )


# Configure CORS with detailed logging
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8007",  # Your frontend server
        "http://127.0.0.1:8007",
        "file:///",  # If opening directly from file system
        "*"  # Use * cautiously in production
    ],
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Initialize recipe storage
recipe_storage = RecipeStorage()


# GET all recipes with error handling
@app.get("/recipes", response_model=List[RecipeResponse])
async def get_recipes():
    logger.info("Received GET request for recipes")
    try:
        recipes = recipe_storage.get_all_recipes()
        logger.debug(f"Returning {len(recipes)} recipes")
        return recipes
    except Exception as e:
        logger.error(f"Error in get_recipes: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# POST new recipe with comprehensive error handling
@app.post("/recipes", response_model=RecipeResponse)
async def create_or_update_recipe(recipe: RecipeData):
    logger.info(f"Received POST request to create recipe: {recipe}")
    try:
        # Validate input
        if not recipe.name:
            logger.warning("Attempted to create recipe with empty name")
            raise HTTPException(status_code=400, detail="Recipe name cannot be empty")

        # Create recipe
        created_recipe = recipe_storage.create_recipe(recipe)
        logger.info(f"Successfully created recipe: {created_recipe}")
        return created_recipe
    except ValidationError as ve:
        logger.error(f"Validation error: {ve}")
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        logger.error(f"Unexpected error in create_recipe: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Optional: Add a simple health check endpoint
@app.get("/health")
async def health_check():
    logger.info("Health check endpoint called")
    return {"status": "healthy"}


# Startup event for additional logging
@app.on_event("startup")
async def startup_event():
    logger.info("Application is starting up")
    logger.info("Recipe Management API is ready to accept requests")