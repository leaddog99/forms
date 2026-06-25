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
