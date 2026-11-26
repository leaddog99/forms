"""Find the printed photograph of the finished dish on a photographed cookbook
page and cut it out, so a photo extract gets a real dish image as its hero
instead of a picture of the page (curator, 2026-11-25).

One vision call with a forced tool reply. The call decides three things: is a
dish photograph there at all (a chef's portrait is not one), is it whole (a
sliver cut off by the fold or the edge of the frame is not usable), and where
it sits. Only a whole photograph is cropped. Anything else returns None and the
hero stays empty for the editor's image tool.

MODEL: trialled 2026-11-25 on four real pages. Both models made the same
found / whole decisions, but the lighter one cut the box too narrow (lost half
the plate); this one cut at the fold. One real crop is thin evidence - recheck
if crops look wrong.
"""
from __future__ import annotations

import base64
import io
import uuid
from typing import Any, Callable, Optional

METHOD_ID = "dish_photo_crop_v1"
MODEL = "claude-opus-4-8"
_MAX_SIDE = 1568          # the long side sent to the model
_MIN_SIDE = 300           # a crop smaller than this is no hero
_MIN_AREA = 0.08          # ... nor one under 8% of the page photo

_TOOL = {
    "name": "dish_photo",
    "description": "Report where the photograph of the finished dish sits in this picture of a cookbook page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean",
                      "description": "true only if a photograph of the finished dish is visible"},
            "complete": {"type": "boolean",
                         "description": "true only if the dish photograph is essentially whole in the frame, "
                                        "not a sliver cut off by the edge of the picture or the fold of the book"},
            "what": {"type": "string", "description": "one short phrase: what the photograph shows"},
            "x0": {"type": "integer"}, "y0": {"type": "integer"},
            "x1": {"type": "integer"}, "y1": {"type": "integer"},
        },
        "required": ["found", "complete", "what"],
    },
}

_SYSTEM = (
    "You are looking at a phone photo of a cookbook or magazine page. Find the printed photograph of "
    "the FINISHED DISH, if there is one. A portrait of a chef or any person is not a dish photograph. "
    "Decorative drawings, icons, the table under the book and fingers holding the page are not either. "
    "If a dish photograph is present, give its bounding box in pixels of the image as sent "
    "(x0,y0 = top left, x1,y1 = bottom right), tight to the printed photograph and excluding the text "
    "page, the book's edge and the background. If only a sliver of a dish photograph is visible, "
    "report found=true, complete=false. When in doubt, found=false."
)


def find_and_crop(image_bytes: bytes, *, create: Optional[Callable[..., Any]] = None,
                  log: Callable[[str], None] = print) -> tuple[Optional[bytes], dict]:
    """-> (JPEG bytes of the dish photograph, or None; info).
    info = {method, model, found, complete, what, box?, reason?}. Never raises."""
    info: dict = {"method": METHOD_ID, "model": MODEL, "found": False, "complete": False, "what": ""}
    try:
        from PIL import Image, ImageOps
        full = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        fw, fh = full.size
        scale = min(1.0, _MAX_SIDE / float(max(fw, fh)))
        sent = full if scale == 1.0 else full.resize((max(1, round(fw * scale)), max(1, round(fh * scale))))
        sw, sh = sent.size
        buf = io.BytesIO()
        sent.save(buf, format="JPEG", quality=85)
        if create is None:
            import llm
            create = llm.create
        resp = create(operation="dish_photo_crop", model=MODEL, max_tokens=400, system=_SYSTEM,
                      tools=[_TOOL], tool_choice={"type": "tool", "name": _TOOL["name"]},
                      messages=[{"role": "user", "content": [
                          {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                       "data": base64.b64encode(buf.getvalue()).decode()}},
                          {"type": "text", "text": f"The image is {sw} pixels wide and {sh} pixels high."}]}])
        data = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if not isinstance(data, dict):
            info["reason"] = "no readable answer"
            return None, info
        info.update(found=bool(data.get("found")), complete=bool(data.get("complete")),
                    what=str(data.get("what") or "")[:200])
        if not (info["found"] and info["complete"]):
            info["reason"] = "no dish photograph" if not info["found"] else "only part of the dish photograph is in frame"
            return None, info
        try:
            x0, y0, x1, y1 = (int(data[k]) for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            info["reason"] = "no usable box"
            return None, info
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(sw, x1), min(sh, y1)
        box = tuple(round(v / scale) for v in (x0, y0, x1, y1))
        w, h = box[2] - box[0], box[3] - box[1]
        if min(w, h) < _MIN_SIDE or (w * h) < _MIN_AREA * fw * fh:
            info["reason"] = "the dish photograph is too small to use"
            return None, info
        info["box"] = list(box)
        out = io.BytesIO()
        full.crop(box).save(out, format="JPEG", quality=92)
        return out.getvalue(), info
    except Exception as e:  # noqa: BLE001 - a hero is optional; never fail the extract over it
        log(f"[DISH-CROP] skipped: {type(e).__name__}: {str(e)[:160]}")
        info["reason"] = f"could not be run ({type(e).__name__})"
        return None, info


def crop_and_store(image_bytes: bytes, *, create: Optional[Callable[..., Any]] = None,
                   log: Callable[[str], None] = print) -> tuple[Optional[str], dict]:
    """find_and_crop, then store the crop like any uploaded hero. -> (url or None, info)."""
    crop, info = find_and_crop(image_bytes, create=create, log=log)
    if not crop:
        log(f"[DISH-CROP] no hero: {info.get('reason')} ({info.get('what')})")
        return None, info
    try:
        from input.pipeline.image_pipeline import standardize_and_meta
        from input.pipeline.image_store import get_image_store
        processed, meta = standardize_and_meta(crop, source_url=None, localized=True)
        fmt = (meta.get("format") or "webp") if processed else "jpeg"
        url = get_image_store().put(f"heroes/dishcrop_{uuid.uuid4()}.{'jpg' if fmt == 'jpeg' else fmt}",
                                    processed or crop, content_type=f"image/{fmt}")
        info["meta"] = meta if processed else {}
        log(f"[DISH-CROP] hero cut from the page photo: {info.get('what')} box={info.get('box')} -> {url}")
        return url, info
    except Exception as e:  # noqa: BLE001
        log(f"[DISH-CROP] store failed: {type(e).__name__}: {str(e)[:160]}")
        info["reason"] = f"could not be stored ({type(e).__name__})"
        return None, info
