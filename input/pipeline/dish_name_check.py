"""Typo guard at dish creation.

A dish's `name` is an immutable join key (a dozen tables hang off it), so a
misspelling made at creation cannot be cleanly renamed afterwards: "Seared
Scallope" (2026-11-27) had to be papered over with a display name, and "Shrimp
Sti Fry" / "Vegetable Sti Fry" were cancelled seconds after they started. The
search engine forgives the typo and the title screen passed 48 of 48, so nothing
downstream ever says a word.

One small model call reads the NAME and says whether it is a misspelling of a
known dish or food, and if so what the right spelling is. A dictionary cannot do
this: Kolokithopita, Daigakuimo and Cock-a-Leekie are correct and look like
typos. The model is told to pass anything it is not sure about, and a
transliteration or regional spelling is not a typo.

ADVISORY ONLY. The create form shows the suggestion and lets the curator use it
or create the name as typed; a failed call never blocks a create.
Setting `dish_name_spellcheck` (System -> Search), default on.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable, Optional

METHOD_ID = "dish_name_spellcheck_v1"
MODEL = "claude-haiku-4-5"
SETTING = "dish_name_spellcheck"

_SYSTEM = (
    "You proofread ONE dish name typed by a cookbook curator who is about to add it to a catalog. "
    "Decide whether the name contains a SPELLING MISTAKE or slip of the keyboard - a wrong, missing, "
    "doubled or swapped letter in a word of a real dish or food name ('Seared Scallope' -> 'Seared "
    "Scallops', 'Shrimp Sti Fry' -> 'Shrimp Stir Fry', 'Chiken Parmesan' -> 'Chicken Parmesan').\n"
    "These are NOT mistakes, leave them alone:\n"
    "- a foreign, regional or transliterated dish name, however unfamiliar it looks (Kolokithopita, "
    "Daigakuimo, Cock-a-Leekie, Mirlitons, Ployes, Pastitsio, Shorabet Adas);\n"
    "- an accepted alternative spelling or transliteration (Yoghurt/Yogurt, Hummus/Houmous, "
    "Pastitsio/Pastitcio, Chili/Chilli, Doughnut/Donut);\n"
    "- missing accents, capitalisation, punctuation, hyphens, or a qualifier in brackets;\n"
    "- an unusual but correctly spelled combination of words.\n"
    "Only report a mistake when you are confident what the intended name is. If you are not sure, "
    "say the name is fine. Keep the curator's wording and capitalisation style; change only the "
    "misspelled letters. Answer with ONLY a JSON object: "
    '{"typo": true|false, "suggestion": "<the corrected full name, only when typo is true>"}'
)

_cache: dict[str, dict] = {}


def _bare(s: str) -> str:
    """Letters and digits only, accents folded: what a spelling change must alter."""
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]+", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def enabled() -> bool:
    try:
        from input.pipeline.system_config import get_setting
        return bool(get_setting(SETTING, True))
    except Exception:
        return True


def check(name: str, *, create: Optional[Callable[..., Any]] = None,
          log: Callable[[str], None] = print) -> Optional[dict]:
    """-> {"suggestion": "<corrected name>", "method": ...} when `name` looks
    misspelled, else None. None also on any failure (advisory: never blocks)."""
    name = (name or "").strip()
    if len(name) < 4:
        return None
    key = name.lower()
    if create is None and key in _cache:
        return _cache[key] or None
    try:
        if create is None:
            import llm
            _create = llm.create
        else:
            _create = create
        msg = _create(operation="dish_name_spellcheck", model=MODEL, max_tokens=120, system=_SYSTEM,
                      messages=[{"role": "user", "content": f"DISH NAME: {name}"}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:  # noqa: BLE001 - advisory
        log(f"[DISH-NAME] spelling check skipped ({type(e).__name__}: {str(e)[:120]})")
        return None
    out: dict = {}
    sug = str(data.get("suggestion") or "").strip()
    # A "correction" that only changes case, accents or punctuation is not a typo.
    if data.get("typo") is True and sug and _bare(sug) != _bare(name) and len(sug) <= len(name) + 12:
        out = {"suggestion": sug, "method": METHOD_ID, "model": MODEL}
        log(f"[DISH-NAME] {name!r} looks misspelled; suggested {sug!r}")
    if create is None:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = out
    return out or None
