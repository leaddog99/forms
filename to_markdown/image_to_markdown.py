"""Image -> canonical markdown adapter.

Vision-LLM OCR step. Takes a path to a recipe image (cookbook page,
handwritten card, magazine clipping, webpage screenshot) and produces
markdown shaped for `extract.markdown_to_recipe`.

There is no JSON-LD branch here — images can't ship structured data, so
the downstream extract call falls back to its body-only path. The
markdown shape matches `html_to_markdown` minus the JSON-LD block.

Vision model is claude-sonnet-4-6, not the haiku used elsewhere — OCR
errors on tricky inputs (handwriting, faded scans, busy magazine
layouts) pass silent validation and ship wrong recipes, so the
quality premium is worth it for the rarely-called vision path.
"""
import base64
import io
import time
from mimetypes import guess_type
from typing import Optional

import anthropic
from PIL import Image


_anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


# Anthropic vision rejects base64-encoded images over 5MB. The CAP
# applies to the base64 payload, not the raw bytes — base64 expands
# by 4/3, so a 4MB raw JPEG becomes ~5.3MB base64 and gets rejected.
# Set the trigger at 3.7MB raw (≈ 5MB base64) with a small margin.
# iPhone JPEGs from the Photos picker frequently land in the 3-5MB
# raw range, exactly the band that used to slip past a raw-byte check.
_ANTHROPIC_B64_CAP = 5_242_880          # Anthropic's documented limit
_DOWNSCALE_RAW_THRESHOLD = 3_700_000    # raw bytes — ~4.93MB after base64
_MAX_LONG_EDGE = 2000
_DOWNSCALE_JPEG_QUALITY = 85


def _base64_size(raw_len: int) -> int:
    """Exact byte length a `raw_len`-byte payload occupies after
    base64 encoding (no newlines): 4 chars per 3 bytes, padded to a
    multiple of 4."""
    return ((raw_len + 2) // 3) * 4


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


def _downscale_to_jpeg(image_bytes: bytes) -> bytes:
    """Re-encode the image as JPEG with the long edge capped at
    `_MAX_LONG_EDGE` pixels. Used when the original is too big for
    Anthropic's 5MB base64 cap. HEIC/PNG/etc. all collapse to JPEG —
    text OCR doesn't benefit from lossless and the cap is what matters.
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        # Flatten alpha onto white so JPEG (no alpha channel) doesn't
        # blacken transparent backgrounds.
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        long_edge = max(w, h)
        if long_edge > _MAX_LONG_EDGE:
            scale = _MAX_LONG_EDGE / long_edge
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_DOWNSCALE_JPEG_QUALITY, optimize=True)
        return buf.getvalue()


def image_to_base64(image_path: str) -> tuple[str, str]:
    """Read image bytes, return (media_type, base64-encoded data).

    Anthropic vision rejects base64 payloads over 5MB. We trigger
    downscale at `_DOWNSCALE_RAW_THRESHOLD` raw bytes, which after
    base64 expansion (≈4/3) lands just under the 5MB cap with a
    safety margin. Below the threshold the original bytes pass
    through with their detected media_type.
    """
    with open(image_path, "rb") as f:
        raw = f.read()

    media_type, _ = guess_type(image_path)
    if not media_type or not media_type.startswith("image/"):
        media_type = "image/jpeg"

    if len(raw) > _DOWNSCALE_RAW_THRESHOLD:
        print(f"     IMAGE: {len(raw):,} raw bytes (~{_base64_size(len(raw)):,} b64) "
              f"exceeds {_DOWNSCALE_RAW_THRESHOLD:,} threshold; "
              f"downscaling to {_MAX_LONG_EDGE}px JPEG q={_DOWNSCALE_JPEG_QUALITY}")
        raw = _downscale_to_jpeg(raw)
        media_type = "image/jpeg"
        print(f"     IMAGE: downscaled to {len(raw):,} raw bytes "
              f"(~{_base64_size(len(raw)):,} b64)")

    # Belt-and-suspenders: if the downscale (or an undersized original
    # that's somehow still too dense) lands above the cap, refuse to
    # send rather than letting Anthropic 400 us with a less-clear error.
    b64_size = _base64_size(len(raw))
    if b64_size > _ANTHROPIC_B64_CAP:
        raise ValueError(
            f"Image still exceeds Anthropic's {_ANTHROPIC_B64_CAP:,}-byte "
            f"base64 cap after downscale ({b64_size:,} bytes). "
            f"Crop the image or reduce its resolution before retrying."
        )

    encoded = base64.b64encode(raw).decode("utf-8")
    return media_type, encoded


def image_to_markdown(image_path: str, *, model: str = "claude-sonnet-4-6",
                      timings: Optional[dict] = None,
                      usage_log: Optional[list] = None) -> str:
    """Vision-OCR an image into markdown. Returns empty string on failure.

    If `timings` is provided it is populated with:
        encode_ms       base64 of the image bytes
        vision_llm_ms   the Claude vision call
    If `usage_log` is provided, one entry is appended with the LLM token
    counts so the caller can journal it.
    """
    from input.pipeline.token_journal import build_usage_entry

    t0 = time.perf_counter()
    media_type, b64 = image_to_base64(image_path)
    t_encode = time.perf_counter()
    if timings is not None:
        timings["encode_ms"] = int((t_encode - t0) * 1000)

    # Streamed to avoid SDK HTTP timeouts on slow vision responses
    # (high-detail magazine pages can produce sizeable markdown).
    with _anthropic_client.messages.stream(
        model=model,
        max_tokens=4096,
        temperature=0.2,
        system=IMAGE_TO_MARKDOWN_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                }},
                {"type": "text", "text": "Convert this recipe image into markdown."},
            ],
        }],
    ) as stream:
        response = stream.get_final_message()

    if timings is not None:
        timings["vision_llm_ms"] = int((time.perf_counter() - t_encode) * 1000)
    if usage_log is not None:
        usage_log.append(build_usage_entry("image_to_markdown", model, response))

    content = next((b.text for b in response.content if b.type == "text"), "")
    return content.strip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m to_markdown.image_to_markdown <image_path>")
        sys.exit(1)
    print(image_to_markdown(sys.argv[1]))