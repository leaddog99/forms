import json
import os
import sys
from datetime import datetime
from recipe_model import RecipeModel
from image_gen_openai import generate_dish_image, generate_ingredient_image

def load_context(context_path):
    with open(context_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_context(context_path, data):
    with open(context_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def enrich_image_field(url_key, data, generated_path):
    recipe = RecipeModel(**data)

    if not recipe.needs_image_generation():
        print(f"✅ No image generation needed for: {url_key}")
        return data, False

    print("🖼️ Generating image...")
    try:
        if recipe.prefers_dish_image():
            image_data = generate_dish_image(recipe)
        else:
            image_data = generate_ingredient_image(recipe)

        filename = f"generated_images/{url_key.replace('/', '_').replace(' ', '_')}.png"
        with open(filename, 'wb') as f:
            f.write(image_data)

        new_image_url = f"/static/{filename}"
        data['image'] = [new_image_url]
        data['history'].append({
            "timestamp": datetime.utcnow().isoformat(),
            "step": "enrich_image",
            "status": "complete",
            "generated": new_image_url
        })
        return data, True

    except Exception as e:
        print(f"❌ Error generating image: {e}")
        data['history'].append({
            "timestamp": datetime.utcnow().isoformat(),
            "step": "enrich_image",
            "status": "error",
            "error": str(e)
        })
        return data, False

def process_batch_context(context_path):
    context = load_context(context_path)
    updated = False

    for url_key, entry in context.items():
        if entry.get('current_status') != 'accepted':
            continue

        print(f"🖼️ Processing: {url_key}")
        enriched, did_update = enrich_image_field(url_key, entry, 'generated_images')
        context[url_key] = enriched
        updated = updated or did_update

    if updated:
        save_context(context_path, context)
        print(f"📁 Context updated: {context_path}")
    else:
        print("✅ All images already present. No update needed.")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python enrich_image.py path/to/context.json")
        sys.exit(1)
    process_batch_context(sys.argv[1])
