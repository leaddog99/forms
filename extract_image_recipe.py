from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from recipe_model import RecipeModel
from sanitize_recipe_data import sanitize_recipe_data
import uvicorn
import tempfile
import os
from extract_content_image_debug import extract_from_image as extract_recipe_from_image_file


app = FastAPI()

# Allow CORS if frontend is served elsewhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/extract-image-recipe")
async def extract_image_recipe(image: UploadFile = File(...)):
    try:
        # Save the uploaded image to a temp file
        suffix = os.path.splitext(image.filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await image.read())
            tmp_path = tmp.name

        # Run the extractor
        raw_recipe = extract_recipe_from_image_file(tmp_path)
        if not raw_recipe:
            raise HTTPException(status_code=400, detail="No recipe data returned.")

        # Sanitize/validate with Pydantic model
        try:
            validated = RecipeModel(**sanitize_recipe_data(raw_recipe))
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=f"Validation failed: {e}")

        return validated.dict()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

if __name__ == "__main__":
    uvicorn.run("extract_image_recipe:app", host="0.0.0.0", port=8006, reload=True)
