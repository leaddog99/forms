import json
import os
import sys
from datetime import datetime, timezone
from recipe_model import RecipeModel
from image_gen_openai import generate_dish_image, generate_ingredient_image


def load_context(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_context(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def process_batch_context(context_path):
    context = load_context(context_path)
    updated = False

    for key, entry in context.items():
        print(f"     🖼️ Processing: {key}")
        if entry.get("current_status") == "rejected":
            continue

        try:
            recipe_data = RecipeModel(**{k: v for k, v in entry.items() if k not in ["history", "current_status"]})
        except Exception as e:
            print(f"     ❌ Error parsing recipe data: {e}")
            continue

        if isinstance(recipe_data, str):
            print("     ❌ Error: RecipeModel initialized as str instead of RecipeModel")
            continue

        if not hasattr(recipe_data, "generate_prompt"):
            print("     ⚠️ Skipping: RecipeModel missing 'generate_prompt' method")
            continue

        if not recipe_data.needs_image_generation():
            print("     ✅  No image generation needed.")
            continue

        image_bytes = None
        try:
            if recipe_data.prefers_dish_image():
                prompt = recipe_data.generate_prompt()
                print(f"     🧠 Prompt: {prompt}")
                image_bytes = generate_dish_image(recipe_data)
            else:
                prompt = recipe_data.generate_prompt()
                print(f"     🧠 Prompt: {prompt}")
                image_bytes = generate_ingredient_image(recipe_data)
        except Exception as e:
            print(f"     ❌ Error generating image: {e}")
            continue

        if image_bytes:
            output_dir = os.path.join("generated-images")
            ensure_dir(output_dir)
            image_filename = f"generated_{key.replace('.', '_')}.png"
            image_path = os.path.join(output_dir, image_filename)
            with open(image_path, "wb") as f:
                f.write(image_bytes)

            context[key].setdefault("image", []).append(image_path)
            context[key]["imageSource"] = image_path
            context[key].setdefault("history", []).append({
                "timestamp": datetime.now().isoformat(),
                "step": "enrich_image",
                "status": "complete",
                "generated": image_path
            })
            updated = True
            print(f"     ✅  Image saved: {image_path}")

    if updated:
        save_context(context_path, context)
        print(f"     📁 Context updated: {context_path}")
    else:
        print("     📭 No updates made.")


if __name__ == "__main__":
    # Program start message with timestamp
    start_time = datetime.now(timezone.utc)
    print(f"\n>>>> Start: enrich_image.py at {start_time.isoformat()}")

    if len(sys.argv) < 2:
        print("Usage: python enrich_image.py <context_path>")
    else:
        process_batch_context(sys.argv[1])

    # Program end message with timestamp
    end_time = datetime.now(timezone.utc)
    print(f">>>> End: enrich_image.py at {end_time.isoformat()}\n")
