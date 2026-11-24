"""The METHOD CHECK for a video whose ingredient list is written and whose
method is spoken (video lane class `ingredients`, ladder rung 1b).

The extractor wrote the method from the captions. Before that recipe takes a
video seat, a SEPARATE model call reads the captions, the written description
and the extracted recipe side by side and answers five specific questions -
not "is this good", which passes almost everything:

  recipe_lesson      the video is a cook seriously teaching THIS recipe - not
                     comedy, a food-travel piece, a challenge, a review or a
                     meme with a recipe attached. Views say nothing about this:
                     such videos are often the most watched and are useless as
                     a recipe source (curator, 2026-11-24).
  ingredients_used   every written ingredient is used in a step, at the written quantity
  cues_present       the cooking steps carry a time, temperature or doneness cue
                     that was actually said
  complete           the method runs from preparation to the finished dish, no gap
  nothing_invented   nothing in the recipe is absent from both the captions and
                     the written description

Pass = all five, read STRICTLY: the library is meant to hold the highest
quality recipes, and a doubtful video is left out rather than let in.
The verdict, the five answers and the reason are stamped on
the saved row (`_master.video.method_check`) with this method id.

LIMIT (known, curator decision 2026-11-24): the judge reads the CAPTIONS, not
the video. A number the captions misheard passes the extraction and this check
alike. The verdicts have not yet been compared with a person watching the
videos - see the note in docs/video-recipes-lane.md.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

METHOD_ID = "video_method_check_v2"
MODEL = "claude-opus-4-8"          # judgment, same tier as cook_audit.MODEL
CHECKS = ("recipe_lesson", "ingredients_used", "cues_present", "complete", "nothing_invented")
_MAX_TRANSCRIPT_CHARS = 60_000

_TOOL = {
    "name": "report_method_check",
    "description": "Report the result of checking an extracted recipe method against the video's captions.",
    "input_schema": {
        "type": "object",
        "properties": {
            **{k: {"type": "object",
                   "properties": {"ok": {"type": "boolean"},
                                  "note": {"type": "string",
                                           "description": "One short sentence. When ok is false, name the "
                                                          "specific ingredient, step or addition."}},
                   "required": ["ok", "note"]} for k in CHECKS},
            "reason": {"type": "string",
                       "description": "One plain sentence a curator can read: why this method passes or fails."},
        },
        "required": [*CHECKS, "reason"],
    },
}

_SYSTEM = (
    "You are checking a recipe that was written from a cooking video. The ingredient list was written "
    "under the video by its creator. The METHOD was not written: it was composed by another model from "
    "the video's captions (often auto-generated, sometimes in another language; the recipe is in English). "
    "A cook will follow this method without watching the video, so it must stand on its own.\n\n"
    "THE STANDARD. This library holds only the highest-quality recipes, and you are its gatekeeper for "
    "video. Popularity is not evidence of quality: many of the most-watched food videos are "
    "entertainment - jokes and banter, a travelogue through street-food stalls, a challenge, a stunt, a "
    "reaction, a restaurant visit, a meme - and are worthless as a recipe source however many people "
    "watch them. Admitting one damages the integrity of the whole collection. Be strict. When you are "
    "in real doubt about any question, answer no: leaving out a decent video costs little, letting in "
    "a poor one costs a great deal.\n\n"
    "Answer five questions, strictly from the material given:\n"
    "0. recipe_lesson - Judging from the captions, is this video a cook deliberately TEACHING this "
    "recipe, start to finish, as its main purpose? Pass only when the instruction is the substance of "
    "the video: the cook explains what to do, how much, and how to tell it is right. A light, friendly "
    "or humorous manner is fine when the teaching is sound. Fail when the cooking is incidental or "
    "played for entertainment: comedy or banter that crowds out the instruction, a food-travel or "
    "street-food piece, eating and reacting, a challenge or stunt, a review, a short that shows a dish "
    "being made without really explaining it, or a method that had to be pieced together from "
    "passing remarks.\n"
    "1. ingredients_used - Is every ingredient in the WRITTEN list used in some step of the method, and "
    "where a step states a quantity, does it agree with the written list? Optional garnishes may be "
    "unused. Fail if a main ingredient never appears in a step or a quantity contradicts the list.\n"
    "2. cues_present - Do the steps that involve heat or waiting (baking, simmering, frying, resting, "
    "chilling) tell the cook how long, how hot, or what to look for, and do those cues appear in the "
    "captions? Steps that need no cue (stir, combine, season) do not count against it. Fail if the main "
    "cooking step has no cue at all, or a cue is not supported by the captions.\n"
    "3. complete - Does the method run in order from preparation to the finished dish with no missing "
    "stage (for example an ingredient that appears already cooked with no step cooking it, or a method "
    "that stops before the dish is finished)?\n"
    "4. nothing_invented - Is every ingredient, quantity, time and temperature in the recipe found in "
    "either the captions or the written description? Ordinary wording changes and unit spelling are "
    "fine. Fail if the recipe states a specific quantity, time, temperature or ingredient that neither "
    "source contains.\n\n"
    "Be exact and unsentimental. Do not fail a sound, carefully taught method merely for brevity or "
    "for a cue the creator never gave on a step that does not need one - but do not excuse real "
    "defects either. In the reason, say plainly what decided it."
)


def check(recipe: dict, transcript: str, description: str, *,
          create: Optional[Callable[..., Any]] = None, log: Callable[[str], None] = print) -> dict:
    """-> {verdict: 'pass'|'fail'|'error', reason, checks: {name: {ok, note}}, method, model}.
    'error' = the check could not be run (no captions, the call failed, an
    unreadable reply); the caller decides what that means for the seat.
    `create` is llm.create by default (injected in tests)."""
    base = {"method": METHOD_ID, "model": MODEL}
    transcript = (transcript or "").strip()
    if not transcript:
        return {**base, "verdict": "error", "reason": "no captions to check the method against", "checks": {}}
    ings, steps = _ingredients(recipe), _steps(recipe)
    if not steps:
        return {**base, "verdict": "fail", "reason": "the extracted recipe has no method steps", "checks": {}}
    user = (
        f"RECIPE NAME: {recipe.get('name') or ''}\n\n"
        "WRITTEN DESCRIPTION UNDER THE VIDEO (the creator's own text):\n"
        f"{(description or '').strip()[:8000] or '(none)'}\n\n"
        "EXTRACTED INGREDIENTS:\n" + "\n".join(f"- {i}" for i in ings) + "\n\n"
        "EXTRACTED METHOD:\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)) + "\n\n"
        "CAPTIONS (what was said in the video):\n" + transcript[:_MAX_TRANSCRIPT_CHARS]
    )
    if create is None:
        import llm
        create = llm.create
    try:
        resp = create(operation="video_method_check", model=MODEL, max_tokens=2500, system=_SYSTEM,
                      tools=[_TOOL], tool_choice={"type": "tool", "name": _TOOL["name"]},
                      messages=[{"role": "user", "content": user}])
        data = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    except Exception as e:  # noqa: BLE001 - any failure of the call is 'could not be run'
        log(f"[VIDEO-CHECK] call failed: {type(e).__name__}: {str(e)[:160]}")
        return {**base, "verdict": "error", "reason": f"the method check could not be run ({type(e).__name__})",
                "checks": {}}
    if not isinstance(data, dict) or any(not isinstance(data.get(k), dict) for k in CHECKS):
        log(f"[VIDEO-CHECK] unreadable reply (stop={getattr(resp, 'stop_reason', '?')}): "
            f"{json.dumps(data, ensure_ascii=False)[:300] if data is not None else 'no tool call'}")
        return {**base, "verdict": "error", "reason": "the method check gave no readable answer", "checks": {}}
    checks = {k: {"ok": bool(data[k].get("ok")), "note": str(data[k].get("note") or "")[:300]} for k in CHECKS}
    failed = [k for k in CHECKS if not checks[k]["ok"]]
    reason = str(data.get("reason") or "").strip()[:400]
    if failed and not reason:
        reason = checks[failed[0]]["note"]
    return {**base, "verdict": "fail" if failed else "pass", "reason": reason, "checks": checks,
            "failed": failed}


def _ingredients(recipe: dict) -> list[str]:
    out = []
    for i in recipe.get("recipeIngredient") or []:
        if isinstance(i, dict):
            out.append(str(i.get("text") or i.get("name") or json.dumps(i, ensure_ascii=False)))
        elif i:
            out.append(str(i))
    return out


def _steps(recipe: dict) -> list[str]:
    out = []
    for s in recipe.get("recipeInstructions") or []:
        if isinstance(s, dict) and s.get("itemListElement"):
            out += [(x.get("text") if isinstance(x, dict) else str(x)) for x in s["itemListElement"]]
        else:
            out.append(s.get("text") if isinstance(s, dict) else str(s))
    return [s for s in out if s]
