import base64
import openai
import io
from PIL import Image

client = openai.OpenAI()

def _generate_image(prompt: str) -> bytes:
    response = client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        size="1024x1024",
        quality="standard",
        n=1
    )
    image_url = response.data[0].url

    # Download image and return bytes
    import requests
    r = requests.get(image_url)
    r.raise_for_status()
    return r.content

def generate_dish_image(recipe_model) -> bytes:
    prompt = f"Photorealistic image of a finished dish of {recipe_model.name}, styled beautifully for a cookbook. White plate, natural lighting, no text."
    return _generate_image(prompt)

def generate_ingredient_image(recipe_model) -> bytes:
    ingredients = recipe_model.recipeIngredient or []
    prompt = (
        "Flat lay photo of ingredients including " +
        ", ".join(ingredients[:6]) +
        ". Natural lighting, white background, no packaging, no text."
    )
    return _generate_image(prompt)
