"""PDF -> canonical markdown adapter.

Mirrors `html_to_markdown` / `image_to_markdown` in shape so the
endpoint layer can dispatch on Content-Type without special-casing
downstream.

Pipeline:
    PDF bytes -> pypdfium2 renders each page to PNG -> vision LLM sees
    every page in a single chat completion (multi-image prompt) ->
    returns combined markdown ready for `markdown_to_recipe`.

Single vision call rather than one-call-per-page because:
  - Cheaper (one prompt overhead instead of N).
  - The model can integrate context across pages (e.g. ingredient list
    on page 1, instructions continuing on page 2).
  - Fewer tokens spent re-describing the doc shape per call.

There is no JSON-LD branch — PDFs don't ship structured data. The
downstream extract uses its body-only path.

Page cap is bounded (default 10) to keep token + cost predictable. For
multi-recipe books a future enhancement could split per recipe and run
each separately; for now a single recipe per PDF is the contract.
"""
import base64
import io
import os
import time
from typing import Optional
from urllib.parse import urlparse, unquote

import anthropic
import pypdfium2 as pdfium
import requests


_anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


PDF_TO_MARKDOWN_PROMPT = """
You are a recipe digitizer reading a multi-page PDF document. Multiple page images follow in order — produce a SINGLE markdown document that captures every recipe-relevant detail visible across all pages.

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
- Treat all pages as ONE document. An ingredient list that starts on page 1 and continues to page 2 is one ingredient list. Don't repeat headers across pages.
- If the PDF contains multiple distinct recipes, pick the FIRST complete one and ignore the rest — say "Additional recipes in this document were not captured." in a Notes line so the user knows.
- Preserve all quantities exactly as written. Do not convert units.
- Ignore page chrome: page numbers, headers/footers, copyright, table-of-contents entries, advertisements.
- If you can't read something, write [illegible] rather than guessing.
- Output ONLY markdown — no preamble, no explanation, no JSON, no wrapping fences.
""".strip()


# Render at 1.5x the screen-rendering scale. Higher gives crisper text for
# OCR; lower keeps token cost down. 1.5 is a reasonable trade for recipe
# text density.
_DEFAULT_RENDER_SCALE = 1.5
_DEFAULT_MAX_PAGES = 10

# Mirrored from image_to_markdown — Anthropic vision rejects per-image
# base64 payloads over 5MB. PDF pages render to PIL Images that we
# encode here; keep them under the cap by saving as JPEG (not PNG;
# PNG is far less compressible for photo-bearing cookbook pages) and
# capping the long edge. Letter-size pages at scale=1.5 land at
# 918x1188 which is well under _MAX_LONG_EDGE; the cap only kicks in
# for unusually large source PDFs.
_ANTHROPIC_B64_CAP = 5_242_880
_MAX_LONG_EDGE = 2000
_JPEG_QUALITY = 85


def _base64_size(raw_len: int) -> int:
    return ((raw_len + 2) // 3) * 4


def _fetch_pdf_bytes(url: str, timeout: int = 30) -> bytes:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def _title_from_url(url: str) -> str:
    """Filename (sans extension), URL-decoded, underscores -> spaces."""
    path = urlparse(url).path
    stem = os.path.splitext(os.path.basename(path))[0]
    return unquote(stem).replace("_", " ").strip()


def _render_pdf_pages_to_jpeg_b64(pdf_bytes: bytes, *,
                                  max_pages: int = _DEFAULT_MAX_PAGES,
                                  scale: float = _DEFAULT_RENDER_SCALE) -> list[str]:
    """Open the PDF, render the first `max_pages` pages to base64 JPEGs.

    Returns a list of raw base64 strings (no `data:` prefix) ready to
    drop into Anthropic's `{"type": "image", "source": {"type":
    "base64", "media_type": "image/jpeg", "data": ...}}` block.

    JPEG (not PNG) and a long-edge cap so a photo-heavy cookbook page
    can't exceed Anthropic's 5MB-per-image cap. Text legibility at
    `_JPEG_QUALITY=85` is comfortable for OCR.
    """
    out: list[str] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            pil_image = page.render(scale=scale).to_pil()
            if pil_image.mode != "RGB":
                pil_image = pil_image.convert("RGB")
            w, h = pil_image.size
            long_edge = max(w, h)
            if long_edge > _MAX_LONG_EDGE:
                k = _MAX_LONG_EDGE / long_edge
                pil_image = pil_image.resize((int(w * k), int(h * k)), pil_image.LANCZOS)
            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            raw = buf.getvalue()
            b64_size = _base64_size(len(raw))
            if b64_size > _ANTHROPIC_B64_CAP:
                raise ValueError(
                    f"PDF page {i+1} still exceeds Anthropic's {_ANTHROPIC_B64_CAP:,}-byte "
                    f"base64 cap after JPEG/downscale ({b64_size:,} bytes). "
                    f"The page may be unusually dense; consider rendering at a lower scale."
                )
            out.append(base64.b64encode(raw).decode("ascii"))
    finally:
        pdf.close()
    return out


def pdf_bytes_to_markdown(
    pdf_bytes: bytes,
    *,
    model: str = "claude-sonnet-4-6",
    max_pages: int = _DEFAULT_MAX_PAGES,
    render_scale: float = _DEFAULT_RENDER_SCALE,
    timings: Optional[dict] = None,
    usage_log: Optional[list] = None,
) -> str:
    """Render PDF pages and OCR them with vision into a single markdown doc.

    Returns the markdown string. Empty string on failure (caller decides
    whether to error). `timings` and `usage_log` are populated in place
    in the same shape as `image_to_markdown`."""
    from input.pipeline.token_journal import build_usage_entry

    t0 = time.perf_counter()
    page_b64s = _render_pdf_pages_to_jpeg_b64(
        pdf_bytes, max_pages=max_pages, scale=render_scale
    )
    t_render = time.perf_counter()
    if timings is not None:
        timings["pdf_render_ms"] = int((t_render - t0) * 1000)
        timings["pdf_pages_rendered"] = len(page_b64s)

    if not page_b64s:
        if timings is not None:
            timings["vision_llm_ms"] = 0
        return ""

    user_content: list = []
    for b64 in page_b64s:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    user_content.append({
        "type": "text",
        "text": (f"Multi-page recipe PDF. {len(page_b64s)} page"
                 f"{'s' if len(page_b64s) > 1 else ''} above in order. "
                 "Produce a single combined markdown document."),
    })

    # Streamed to avoid SDK HTTP timeouts on multi-page vision calls.
    with _anthropic_client.messages.stream(
        model=model,
        max_tokens=4096,
        temperature=0.2,
        system=PDF_TO_MARKDOWN_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        response = stream.get_final_message()

    if timings is not None:
        timings["vision_llm_ms"] = int((time.perf_counter() - t_render) * 1000)
    if usage_log is not None:
        usage_log.append(build_usage_entry("pdf_to_markdown", model, response))

    content = next((b.text for b in response.content if b.type == "text"), "")
    return content.strip()


def pdf_url_to_markdown(
    url: str,
    timings: Optional[dict] = None,
    usage_log: Optional[list] = None,
    *,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> dict:
    """Fetch + render + OCR a PDF URL. Returns the same envelope shape as
    `html_to_markdown` so the endpoint dispatch stays uniform:

        {
            "markdown":    <combined markdown>,
            "source_url":  <input url, unchanged>,
            "title":       <derived from filename>,
            "has_jsonld":  False,
            "jsonld":      None,
        }
    """
    t0 = time.perf_counter()
    pdf_bytes = _fetch_pdf_bytes(url)
    if timings is not None:
        timings["fetch_ms"] = int((time.perf_counter() - t0) * 1000)

    markdown = pdf_bytes_to_markdown(
        pdf_bytes,
        max_pages=max_pages,
        timings=timings,
        usage_log=usage_log,
    )
    return {
        "markdown": markdown,
        "source_url": url,
        "title": _title_from_url(url),
        "has_jsonld": False,
        "jsonld": None,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m to_markdown.pdf_to_markdown <pdf_url_or_path>")
        sys.exit(1)
    arg = sys.argv[1]
    if arg.startswith("http://") or arg.startswith("https://"):
        result = pdf_url_to_markdown(arg)
        print("== title:", result["title"])
        print("== markdown:")
        print(result["markdown"])
    else:
        with open(arg, "rb") as f:
            md = pdf_bytes_to_markdown(f.read())
        print(md)
