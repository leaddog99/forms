"""Image -> canonical markdown adapter.

Vision-LLM OCR step. Takes a path to a recipe image (cookbook page,
handwritten card, magazine clipping, webpage screenshot) and produces
markdown shaped for `extract.markdown_to_recipe`.

There is no JSON-LD branch here — images can't ship structured data, so
the downstream extract call falls back to its body-only path. The
markdown shape matches `html_to_markdown` minus the JSON-LD block.
"""
import base64
import os
import time
from mimetypes import guess_type
from typing import Optional

import openai


openai.api_key = os.getenv("OPENAI_API_KEY")


IMAGE_TO_MARKDOWN_PROMPT = """
You are a recipe digitizer. Given an image of a recipe — a cookbook page, magazine clipping, handwritten note, photographed recipe card, or a screenshot of a webpage — produce clean markdown that captures every recipe-relevant detail visible in the image.

Use this structure:

# Recipe Title

Short description if visible.

## Ingredients
- 2 cups flour
- 1 tsp salt

## Instructions
1. First step.
2. Second step.

## Notes
Tips, variations, or chef's notes if present.

Also include, when visible:
- Prep time, cook time, total time, yield/servings as labeled paragraphs
- Author or source attribution
- Category and cuisine
- Equipment

Rules:
- Preserve all quantities exactly as written. Do not convert units.
- Ignore page chrome: navigation, ads, comments, related-recipe blocks, footers.
- If you can't read something, write [illegible] rather than guessing.
- If the image is a webpage screenshot, treat the recipe content as primary; ignore site navigation and social widgets.
- Output ONLY markdown — no preamble, no explanation, no JSON, no wrapping fences.
""".strip()


def image_to_data_url(image_path: str) -> str:
    mime_type, _ = guess_type(image_path)
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def image_to_markdown(image_path: str, *, model: str = "gpt-4o",
                      timings: Optional[dict] = None,
                      usage_log: Optional[list] = None) -> str:
    """Vision-OCR an image into markdown. Returns empty string on failure.

    If `timings` is provided it is populated with:
        encode_ms       base64 of the image bytes
        vision_llm_ms   the OpenAI vision call
    If `usage_log` is provided, one entry is appended with the LLM token
    counts so the caller can journal it.
    """
    from input.pipeline.token_journal import build_usage_entry

    t0 = time.perf_counter()
    image_url = image_to_data_url(image_path)
    t_encode = time.perf_counter()
    if timings is not None:
        timings["encode_ms"] = int((t_encode - t0) * 1000)

    response = openai.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": IMAGE_TO_MARKDOWN_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Convert this recipe image into markdown."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        max_tokens=4096,
        temperature=0.2,
    )
    if timings is not None:
        timings["vision_llm_ms"] = int((time.perf_counter() - t_encode) * 1000)
    if usage_log is not None:
        usage_log.append(build_usage_entry("image_to_markdown", model, response))
    return (response.choices[0].message.content or "").strip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m to_markdown.image_to_markdown <image_path>")
        sys.exit(1)
    print(image_to_markdown(sys.argv[1]))