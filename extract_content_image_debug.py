import os
import json
import uuid
import base64
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
from mimetypes import guess_type

import openai
from sanitize_recipe_data import sanitize_recipe_data
from recipe_model import RecipeModel

# Load your API key from environment
openai.api_key = os.getenv("OPENAI_API_KEY")

# Dynamically generate system prompt from the model schema
SYSTEM_PROMPT = f"""
You are a culinary data extractor. Given an image of a recipe from a book or magazine, extract all structured data as a JSON object conforming exactly to the schema below. This schema is defined by our RecipeModel.

Output a valid JSON object that matches this structure. DO NOT skip required fields. Use empty strings, empty lists, or null where appropriate.

<SCHEMA>
{json.dumps(RecipeModel.model_json_schema(), indent=2)}
</SCHEMA>
"""

# Save prompt for review/debugging
with open("image_prompt.txt", "w", encoding="utf-8") as f:
    f.write(SYSTEM_PROMPT)

def image_to_data_url(image_path: str) -> str:
    mime_type, _ = guess_type(image_path)
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"

def extract_from_image(image_path: str) -> Any:
    user_prompt = "Extract a complete structured recipe from this image. Return the recipe as strict JSON."

    image_url = image_to_data_url(image_path)

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ],
        max_tokens=4096,
        temperature=0.2
    )

    content = response.choices[0].message.content
    try:
        json_start = content.find('{')
        json_end = content.rfind('}')
        if json_start == -1 or json_end == -1:
            raise ValueError("No valid JSON object boundaries found in GPT response")
        raw_json = content[json_start:json_end + 1]
        json_data = json.loads(raw_json)
        sanitized = sanitize_recipe_data(json_data)
        sanitized["inputImage"] = os.path.basename(image_path)
        return RecipeModel.model_validate(sanitized).model_dump()
    except Exception as e:
        print("❌ Failed to parse/validate GPT response:", e)
        print("🧪 Raw GPT output:\n", content)
        return None

def process_batch_context(context_path: str):
    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    changed = False
    for key, entry in context.items():
        if not key.lower().endswith((".jpg", ".png")):
            continue
        status = entry.get("current_status", "accepted")
        if status != "accepted":
            print(f"🚫 Skipping (status not accepted): {key}")
            continue

        image_path = entry.get("input_image")
        if not image_path:
            print(f"⚠️ No input_image found for {key}, marking as rejected.")
            entry["current_status"] = "rejected"
            entry.setdefault("history", []).append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "step": "extract_content_image",
                "status": "rejected",
                "module": "extract_content_image.py",
                "reason": "Missing input_image field"
            })
            changed = True
            continue

        full_image_path = os.path.abspath(image_path)
        print(f"📥 Extracting recipe from: {full_image_path}")
        result = extract_from_image(full_image_path)
        if result:
            entry.update(result)
            entry["current_status"] = "accepted"
            entry.setdefault("history", []).append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "step": "extract_content_image",
                "status": "accepted",
                "module": "extract_content_image.py"
            })
            changed = True
        else:
            entry["current_status"] = "rejected"
            entry.setdefault("history", []).append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "step": "extract_content_image",
                "status": "rejected",
                "module": "extract_content_image.py",
                "reason": "Validation or parsing failed"
            })
            changed = True

    if changed:
        with open(context_path, "w", encoding="utf-8") as f:
            json.dump(context, f, indent=2)
        print("📁 Context updated:", context_path)
    else:
        print("📭 No changes made to context.")

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python extract_content_image_debug.py path/to/context.json")
        exit(1)
    process_batch_context(sys.argv[1])
