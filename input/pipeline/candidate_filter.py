"""Candidate filters — the per-domain pre-fetch rule surface.

docs/candidate-filters.md, built 2026-09-01 off the barilla run: the domain
403s every direct fetch, so each of its SEMrush-ranked CATEGORY pages
(/en-us/recipe/no-meat-recipes) cost an unblocker credit just to be dropped
post-fetch — while real recipes live under /en-us/recipe/all/. A URL rule
knows that for free, before any spend.

The model copies SEMrush's advanced filter, which the curator uses daily:
rows of {include|exclude, field, criterion, value}, ANDed. A rule is
{keep: [...], drop: [...]} — every keep condition must hold, any drop
condition kills, drop wins on conflict, empty keep = keep all not dropped.

HARD CONSTRAINT (design §no-LLM-per-candidate): runtime evaluation is string
ops on fields we already hold pre-fetch. The LLM appears only in
`compile_rule` — the curator writes plain English ONCE, the model compiles
it to conditions, the compiled rows are shown back for editing. A condition
carries its `author`: curator-authored drops are facts (not overturnable);
llm-authored drops land in the reconsiderable pool (candidate_ledger
`filter-llm`). A curator who edits an llm condition owns it.

Fields (all pre-fetch): url, url_path, url_depth, title, traffic,
traffic_pct, rank. traffic/traffic_pct exist only on the backlinks_file
source — a condition on a missing field is NOT APPLICABLE and neither keeps
nor drops (design §open-questions, stated rather than silent).
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

STRING_FIELDS = ("url", "url_path", "title")
NUMBER_FIELDS = ("url_depth", "traffic", "traffic_pct", "rank")
FIELDS = STRING_FIELDS + NUMBER_FIELDS
STRING_CRITERIA = ("containing", "not containing", "exactly",
                   "starts with", "ends with", "matches")
NUMBER_CRITERIA = (">", ">=", "<", "<=", "=")
AUTHORS = ("curator", "llm")


def parse_rule(raw) -> dict:
    """Decode + shape-check a stored rule. Returns {} for empty/invalid."""
    if not raw:
        return {}
    try:
        rule = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}
    if not isinstance(rule, dict):
        return {}
    out = {"keep": [], "drop": []}
    for side in ("keep", "drop"):
        for c in rule.get(side) or []:
            if not isinstance(c, dict):
                continue
            f = (c.get("field") or "").strip()
            cr = (c.get("criterion") or "").strip()
            if f not in FIELDS:
                continue
            if f in STRING_FIELDS and cr not in STRING_CRITERIA:
                continue
            if f in NUMBER_FIELDS and cr not in NUMBER_CRITERIA:
                continue
            out[side].append({"field": f, "criterion": cr,
                              "value": c.get("value"),
                              "author": c.get("author") if c.get("author") in AUTHORS
                              else "curator"})
    return out if (out["keep"] or out["drop"]) else {}


def _field_value(cand: dict, field: str):
    url = cand.get("url") or ""
    if field == "url":
        return url.lower()
    if field == "url_path":
        try:
            return (urlparse(url).path or "/").lower()
        except Exception:
            return "/"
    if field == "url_depth":
        try:
            return len([s for s in (urlparse(url).path or "").split("/") if s])
        except Exception:
            return None
    if field == "title":
        t = cand.get("title")
        return (t or "").lower() if t is not None else None
    v = cand.get(field)          # traffic / traffic_pct / rank
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _cond_matches(cond: dict, cand: dict):
    """True/False, or None when the condition is NOT APPLICABLE (missing
    field value, bad regex, non-numeric value)."""
    val = _field_value(cand, cond["field"])
    if val is None:
        return None
    want = cond.get("value")
    if cond["field"] in STRING_FIELDS:
        w = str(want or "").lower()
        if not w:
            return None
        cr = cond["criterion"]
        if cr == "containing":
            return w in val
        if cr == "not containing":
            return w not in val
        if cr == "exactly":
            return val == w
        if cr == "starts with":
            return val.startswith(w)
        if cr == "ends with":
            return val.endswith(w)
        if cr == "matches":
            try:
                return re.search(str(want), str(val), re.I) is not None
            except re.error:
                return None
        return None
    try:
        w = float(want)
    except (TypeError, ValueError):
        return None
    cr = cond["criterion"]
    return {"<": val < w, "<=": val <= w, ">": val > w,
            ">=": val >= w, "=": val == w}.get(cr)


def _describe(cond: dict) -> str:
    return f"{cond['field']} {cond['criterion']} {cond.get('value')}"


def evaluate(rule: dict, cand: dict):
    """(keep: bool, reason: str) for one candidate against a parsed rule.

    reason is '' on keep; on drop it is 'filter-curator: <condition>' or
    'filter-llm: <condition>' — the prefix drives ledger overturnability.
    Drop wins on conflict. Not-applicable conditions do nothing.
    """
    if not rule:
        return True, ""
    for cond in rule.get("drop") or []:
        if _cond_matches(cond, cand) is True:
            return False, f"filter-{cond.get('author', 'curator')}: exclude {_describe(cond)}"
    for cond in rule.get("keep") or []:
        m = _cond_matches(cond, cand)
        if m is False:
            return False, f"filter-{cond.get('author', 'curator')}: not {_describe(cond)}"
    return True, ""


COMPILE_MODEL = "claude-sonnet-5"


def compile_rule(text: str, domain: str, sample_urls: list | None = None) -> dict:
    """Plain English -> conditions, ONE call, author='llm' on every row.
    The compiled rows are a DRAFT for the editor — never applied unseen."""
    import llm
    samples = "\n".join(f"- {u}" for u in (sample_urls or [])[:15]) or "(none)"
    prompt = f"""Compile a curator's plain-English candidate filter for the publisher
domain {domain} into structured conditions.

CURATOR'S RULE: {text}

SAMPLE URLS FROM THIS DOMAIN (for grounding path shapes):
{samples}

Available fields: url, url_path, title (strings — criteria: containing, not containing,
exactly, starts with, ends with, matches[regex]) and url_depth, traffic, traffic_pct,
rank (numbers — criteria: > >= < <= =). Values are matched case-insensitively.
Semantics: every "keep" condition must hold; any "drop" condition kills; drop wins.

Return ONLY JSON, no prose:
{{"keep": [{{"field": "", "criterion": "", "value": ""}}],
  "drop": [{{"field": "", "criterion": "", "value": ""}}]}}"""
    llm.enter(recipe_id=f"domain:{domain}", user_id=0)
    msg = llm.create(operation="candidate_filter_compile", model=COMPILE_MODEL,
                     max_tokens=4000, messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("compiler reply unusable — run again")
    rule = json.loads(m.group(0))
    for side in ("keep", "drop"):
        for c in rule.get(side) or []:
            c["author"] = "llm"
    parsed = parse_rule(rule)
    if not parsed:
        raise ValueError("compiled to no valid conditions — rephrase the rule")
    return parsed
