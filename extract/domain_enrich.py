"""Domain master enrichment — a quick Haiku call that profiles a source site.

Given a domain (host), produce the editorial/provenance fields the domain
editor would otherwise be filled in by hand:

    story          1-2 sentence bio of the publisher
    language       primary publishing language (ISO 639-1)
    country        home country of the publisher
    cuisine_focus  niche/specialty cuisine, or "" for general sites
    logo_url       brand logo (derived deterministically, see below)

Mirrors extract/identity_card.py: Anthropic Haiku + tool_use for structured
output, token-journaled, never raises (the endpoint degrades gracefully).

The LLM is NOT asked for the logo URL — models hallucinate links. Instead we
hand back Clearbit's logo API for the registrable domain, which returns a real
brand logo for known publishers and a 404 the curator can simply clear if the
site isn't covered. The editor shows the URL so it can be eyeballed before save.
"""

from __future__ import annotations

import hashlib
from typing import Optional

import anthropic

from input.pipeline.url_utils import root_domain

import llm  # central LLM gateway — auto-journals usage to bcc_token_journal
_anthropic_client = anthropic.Anthropic()

_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 500
_TEMPERATURE = 0.2

DOMAIN_PROFILE_TOOL = {
    "name": "submit_domain_profile",
    "description": "Submit the structured profile of a recipe/food website.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["story", "language", "country", "cuisineFocus", "recognized"],
        "properties": {
            "story": {
                "type": "string",
                "description": (
                    "A 1-2 sentence editorial bio of this site/publisher: what it is, "
                    "its focus/cuisine, and who is behind it. When homepage content is "
                    "provided, GROUND the bio in it (paraphrase — don't quote the tagline "
                    "verbatim). Factual and brand-neutral. Don't invent specific people, "
                    "awards, or history the content doesn't support. Only empty if there "
                    "is NO homepage content AND you don't recognize the site."
                ),
            },
            "language": {
                "type": "string",
                "description": (
                    "Primary publishing language as an ISO 639-1 code (en, el, "
                    "it, fr, es, de, ...). Infer from the known site or the TLD "
                    "(.gr -> el, .it -> it). Empty string if unsure."
                ),
            },
            "country": {
                "type": "string",
                "description": (
                    "Home country of the publisher, full English name (e.g. "
                    "'United States', 'Greece', 'United Kingdom'). Empty string "
                    "if unsure."
                ),
            },
            "cuisineFocus": {
                "type": "string",
                "description": (
                    "If the site specializes in a cuisine or niche, name it "
                    "(e.g. 'Greek', 'Thai', 'Italian', 'Baking', 'Vegan'). This "
                    "is a HINT about the publisher, not authoritative recipe "
                    "ethnicity. Empty string for general / multi-cuisine sites — "
                    "do not force a niche onto a generalist site."
                ),
            },
            "recognized": {
                "type": "boolean",
                "description": (
                    "true if you genuinely recognize this specific site; false "
                    "if you are guessing from the domain name alone."
                ),
            },
        },
    },
}

_SYSTEM_PROMPT = (
    "You are a culinary-web librarian. Profile a food/recipe website. When the user "
    "supplies the site's HOMEPAGE CONTENT, GROUND your profile in it — describe the "
    "ACTUAL site (its focus, cuisine, who runs it) from that content rather than from "
    "memory; that is the common case and you should produce a real `story` for it. "
    "Without content, fall back to what you reliably know and prefer empty strings over "
    "invented facts. Set `recognized` true only when you genuinely know the brand from "
    "training (not merely from the supplied content). Don't invent people/awards/history "
    "the content doesn't support. Output ONLY through the submit_domain_profile tool."
)

DOMAIN_ENRICH_PROMPT_VERSION = hashlib.sha256(
    _SYSTEM_PROMPT.encode("utf-8")
).hexdigest()[:12]


def _logo_url_for(host: str) -> str:
    """Deterministic brand-logo URL via Clearbit's logo API, keyed on the
    registrable domain. No API key needed; returns a real logo for known
    brands, 404 otherwise (the curator clears it if it doesn't load)."""
    root = root_domain("http://" + host) or host
    return f"https://logo.clearbit.com/{root}" if root else ""


def _homepage_snippet(host: str, max_chars: int = 1500) -> str:
    """Fetch the site's HOMEPAGE and pull a grounding snippet (title + site name + meta/
    og description) so the LLM profiles the REAL site instead of guessing from the name
    (Haiku doesn't know small blogs → empty 'story'). Honors the domain's fetch policy
    (unblocker/render) so blocked sites still resolve. Best-effort: '' on failure → the
    LLM falls back to name-only profiling."""
    import re
    try:
        from to_markdown.html_to_markdown import fetch_with_full_fallback
        from input.pipeline import domains_lib
        import sqlite3
        unblock = render = False
        try:
            with sqlite3.connect("recipes.db", timeout=10) as c:
                row = domains_lib.get_domain(c, host) or {}
            unblock = (row.get("fetch_strategy") or "") == "unblocker"
            render = bool(row.get("render_required"))
        except Exception:
            pass
        res = fetch_with_full_fallback("https://" + host + "/", unblocker=unblock,
                                       render=render, try_wayback=False)
        resp = res[0] if isinstance(res, tuple) else res
        html = getattr(resp, "text", "") or ""
        if not html:
            return ""

        def _m(pat):
            m = re.search(pat, html, re.I | re.S)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        title = _m(r"<title[^>]*>(.*?)</title>")
        site = _m(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\'](.*?)["\']')
        desc = (_m(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']')
                or _m(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']')
                or _m(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']'))
        parts = []
        if title: parts.append("Page title: " + title)
        if site:  parts.append("Site name: " + site)
        if desc:  parts.append("Meta description: " + desc)
        return ("\n".join(parts))[:max_chars]
    except Exception as e:
        print(f"[DOMAIN-ENRICH] homepage snippet failed: {type(e).__name__}: {e}")
        return ""


def enrich_domain(domain: str, *, display_name: str = "",
                  usage_log: Optional[list] = None) -> Optional[dict]:
    """Profile a domain via Haiku. Returns a dict of suggested fields
    (story, language, country, cuisine_focus, logo_url, recognized) or None
    on failure. Never raises — the endpoint falls back to leaving fields as-is.
    The result is a SUGGESTION; the caller/editor reviews before saving."""
    host = (domain or "").strip().lower()
    if not host:
        return None

    lines = [f"Domain: {host}"]
    root = root_domain("http://" + host)
    if root and root != host:
        lines.append(f"Registrable domain: {root}")
    if display_name:
        lines.append(f"Known display name: {display_name}")
    snippet = _homepage_snippet(host)
    if snippet:
        lines.append("\n--- Homepage content (GROUND your profile in THIS, not memory) ---")
        lines.append(snippet)
    lines.append("Profile this food/recipe website.")
    user_prompt = "\n".join(lines)

    try:
        response = llm.create(
            operation="domain_enrich", model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[DOMAIN_PROFILE_TOOL],
            tool_choice={"type": "tool", "name": "submit_domain_profile"},
        )
    except Exception as e:
        print(f"[DOMAIN-ENRICH] LLM call failed: {type(e).__name__}: {e}")
        return None
    # usage auto-journaled by the gateway (operation="domain_enrich").

    tool_input = next(
        (b.input for b in response.content
         if getattr(b, "type", "") == "tool_use"
         and getattr(b, "name", "") == "submit_domain_profile"),
        None,
    )
    if not isinstance(tool_input, dict):
        return None

    return {
        "story": (tool_input.get("story") or "").strip(),
        "language": (tool_input.get("language") or "").strip(),
        "country": (tool_input.get("country") or "").strip(),
        "cuisine_focus": (tool_input.get("cuisineFocus") or "").strip(),
        "logo_url": _logo_url_for(host),
        "recognized": bool(tool_input.get("recognized")),
    }


# ---------------------------------------------------------------------------
#  Deep enrich — Moz V3 FACTS + a stronger LLM RESEARCH call → a rich `profile`
# ---------------------------------------------------------------------------
_DEEP_MODEL = "claude-sonnet-4-6"   # a capable "research" call, not the quick Haiku blurb
_DEEP_MAX_TOKENS = 1600

_DEEP_PROFILE_TOOL = {
    "name": "submit_domain_deep_profile",
    "description": "Submit a rich, researched profile of a recipe/food publisher.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["profile", "story", "knownFor", "cuisineFocus", "country", "language", "recognized"],
        "properties": {
            "profile": {
                "type": "string",
                "description": (
                    "A rich 2-4 PARAGRAPH editorial profile of this publisher: what it is "
                    "and its history/origin, the people/chefs/editors behind it and their "
                    "credentials, its editorial voice and approach, and the dishes/techniques "
                    "it is genuinely KNOWN and authoritative for. GROUND every specific claim "
                    "in the provided homepage content and the Moz signals (ranking keywords = "
                    "what they actually rank #1 for; brand authority + referring domains = "
                    "their reach). Do NOT invent specific founders, awards, dates, or history "
                    "that the evidence does not support — if unknown, describe what IS "
                    "supported. Factual and brand-neutral (no marketing superlatives). Plain "
                    "prose, no headings."
                ),
            },
            "story": {"type": "string",
                      "description": "A 1-2 sentence distillation of `profile` (the short bio)."},
            "knownFor": {
                "type": "array", "items": {"type": "string"},
                "minItems": 3, "maxItems": 6,
                "description": (
                    "3-6 short phrases, DEMAND-RANKED (highest real-world search demand "
                    "first), each naming ONE thing this site is genuinely BEST KNOWN FOR. "
                    "Phrase each the way a cook would say it ('Slow-cooker comfort food', "
                    "'No-knead sourdough baking', 'Greek village cooking') — not keyword "
                    "soup. GROUND every phrase in the ranking keywords (their search "
                    "volumes are the world literally asking this site for those things) "
                    "plus the homepage. No generic filler ('great recipes', 'easy meals') "
                    "unless the demand data truly says so."
                ),
            },
            "cuisineFocus": {"type": "string",
                             "description": "Niche/specialty cuisine or focus, or '' for general sites. "
                                            "Infer from the ranking keywords + homepage."},
            "ethnicity": {"type": "string",
                          "description": "The publisher's cultural/culinary origin if clearly specialized "
                                         "(e.g. 'Greek', 'Italian'), else ''."},
            "country": {"type": "string", "description": "Home country of the publisher."},
            "language": {"type": "string", "description": "Primary publishing language, ISO 639-1."},
            "recognized": {"type": "boolean",
                           "description": "True if you have real knowledge of this publisher (not "
                                          "just guessing from the domain)."},
        },
    },
}

_DEEP_SYSTEM = (
    "You are a food-media analyst writing a factual, brand-neutral profile of a recipe/food "
    "publisher for an internal editorial database. You are given the site's homepage content "
    "and hard Moz SEO signals — especially the keywords it ranks #1 for, which reveal what it "
    "is genuinely authoritative on. Write a rich profile GROUNDED in that evidence. Never "
    "fabricate specific people, awards, dates, or history the evidence doesn't support; when a "
    "detail is unknown, stay with what IS supported. Call the submit_domain_deep_profile tool once."
)


def deep_enrich_domain(domain: str, *, display_name: str = "") -> Optional[dict]:
    """Deep domain enrich: Moz V3 FACTS (brand authority, referring domains, ranking
    keywords) + a stronger LLM RESEARCH call grounded on those facts + the homepage.
    Returns suggested fields incl. a multi-paragraph `profile` and the Moz facts, or
    None on LLM failure. Never raises. The Moz facts degrade gracefully to None/[] if
    the V3 API is unavailable — the LLM profile still runs on the homepage alone."""
    host = (domain or "").strip().lower()
    if not host:
        return None
    root = root_domain("http://" + host) or host

    # 1) Moz V3 facts (best-effort; each never raises).
    from input.pipeline import moz_v3
    ba = moz_v3.brand_authority(root)
    metrics = moz_v3.site_metrics(root) or {}
    keywords = moz_v3.ranking_keywords(root, limit=15)

    # 2) Homepage grounding.
    snippet = _homepage_snippet(host)

    # 3) Build the grounded research prompt.
    lines = [f"Domain: {host}", f"Registrable domain: {root}"]
    if display_name:
        lines.append(f"Known display name: {display_name}")
    facts = []
    if ba is not None:
        facts.append(f"Brand Authority: {ba}/100 (how strongly people search this brand by name)")
    if metrics.get("referring_domains") is not None:
        facts.append(f"Referring domains: {metrics['referring_domains']:,} (sites linking here)")
    if metrics.get("domain_authority") is not None:
        facts.append(f"Domain Authority: {metrics['domain_authority']}/100")
    if keywords:
        kw = ", ".join(f"{k['keyword']} (#{k['rank']}, {k['volume']:,}/mo)"
                       for k in keywords[:15] if k.get("keyword"))
        facts.append(f"Ranks #1-ish on Google for: {kw}")
    if facts:
        lines.append("\n--- Moz SEO signals (hard data — GROUND the profile in these) ---")
        lines.extend(facts)
    if snippet:
        lines.append("\n--- Homepage content (GROUND your profile in THIS, not memory) ---")
        lines.append(snippet)
    lines.append("\nWrite the researched profile of this food/recipe publisher.")

    try:
        response = llm.create(
            operation="domain_deep_enrich", model=_DEEP_MODEL, max_tokens=_DEEP_MAX_TOKENS,
            system=_DEEP_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            tools=[_DEEP_PROFILE_TOOL],
            tool_choice={"type": "tool", "name": "submit_domain_deep_profile"},
        )
    except Exception as e:
        print(f"[DOMAIN-DEEP-ENRICH] LLM call failed: {type(e).__name__}: {e}")
        return None

    ti = next((b.input for b in response.content
               if getattr(b, "type", "") == "tool_use"
               and getattr(b, "name", "") == "submit_domain_deep_profile"), None)
    if not isinstance(ti, dict):
        return None

    return {
        "profile": (ti.get("profile") or "").strip(),
        "story": (ti.get("story") or "").strip(),
        "cuisine_focus": (ti.get("cuisineFocus") or "").strip(),
        "ethnicity": (ti.get("ethnicity") or "").strip(),
        "country": (ti.get("country") or "").strip(),
        "language": (ti.get("language") or "").strip(),
        "logo_url": _logo_url_for(host),
        "recognized": bool(ti.get("recognized")),
        # Moz FACTS passed straight through (curator sees + saves them).
        "brand_authority": ba,
        "referring_domains": metrics.get("referring_domains"),
        "domain_authority": metrics.get("domain_authority"),
        "ranking_keywords": keywords,
        # Demand-ranked "best known for" phrases — the keyword pills DISTILLED
        # into the site's identity. Feeds the domain form display and (planned)
        # a known-for embedding shared with recipe space for recipe commentary.
        "known_for": [s.strip() for s in (ti.get("knownFor") or [])
                      if isinstance(s, str) and s.strip()],
    }
