#!/usr/bin/env python
"""Export saved recipes as Markdown files.

Written to answer a question we have never tested: **is a library of our recipes
actually useful to reason across?** The pitch for a paid tier leans on synthesis
over the collection a person built — "plan a week with no repeated protein",
"what do these five carbonaras disagree about" — and none of that has been tried.
Dumping real recipes to .md and dropping them into a notebook tool costs nothing
and answers it before anything gets built.

It is also just useful. Export has to exist eventually (never gate the exit), and
this is the smallest honest version of it.

THE STRUCTURE IS THE POINT. A general notebook reads a recipe as prose; we hold it
parsed — ingredient roles, equipment with sizes, cuisine, technique, timings. The
default `structured` style writes that out so a model can use it. `--style plain`
writes an ordinary recipe with none of it, so the two can be compared directly.
If structured does not beat plain, the whole "our data is the moat" argument needs
revisiting, and better to learn that from a test than from a launch.

Usage
-----
    python -m scripts.export_markdown --master --limit 20
    python -m scripts.export_markdown --user 5 --limit 20 --style plain
    python -m scripts.export_markdown --master --ids c60b0d5a,9f31ab77 --out ./nb

Notes
-----
* Read-only. Opens the DB in ro mode and never writes to it.
* Vendor and authority internals (`_scoring`) are never exported — they are ours,
  not the reader's, and they are noise in a notebook. See the no-vendor-names rule.
* Editorial opinion is off by default (`--include-editorial` to add it): it is our
  voice, and leaving it out keeps the test about the recipe data itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "recipes.db")

# ---------------------------------------------------------------- helpers


def _slug(text: str, maxlen: int = 60) -> str:
    """Filesystem-safe, readable, ASCII-only filename stem.

    ASCII-only on purpose. `\\w` matches Unicode, so a Chinese title produced a
    filename of CJK characters — which the files themselves handle fine, but which
    breaks the moment anything prints the path to a Windows console (cp1252) or the
    directory is zipped and moved. The title is preserved in full inside the file;
    only the filename is folded. A title with no ASCII at all falls back to the id.
    """
    s = (text or "").lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return (s[:maxlen].strip("-") or "recipe")


def _say(msg: str) -> None:
    """Print without letting an un-encodable character kill the run — the export
    itself is fine, it is only the console that cannot render the name."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(msg.encode(enc, "replace").decode(enc, "replace"))


def _iso_duration_to_human(d: Optional[str]) -> str:
    """PT1H30M -> '1 hr 30 min'. Returns '' for anything unparseable — an
    unreadable duration is worse than none."""
    if not d or not isinstance(d, str):
        return ""
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", d.strip().upper())
    if not m:
        return ""
    days, hours, mins, _secs = (int(x) if x else 0 for x in m.groups())
    hours += days * 24
    parts = []
    if hours:
        parts.append(f"{hours} hr")
    if mins:
        parts.append(f"{mins} min")
    return " ".join(parts)


def _text_of(step: Any) -> str:
    """Instructions are HowToStep dicts, but older/hand-typed rows hold plain
    strings. Accept both rather than dropping half the corpus."""
    if isinstance(step, str):
        return step.strip()
    if isinstance(step, dict):
        return (step.get("text") or step.get("name") or "").strip()
    return ""


def _clean(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def _yaml_scalar(v: str) -> str:
    """Quote only when needed; keep front matter readable."""
    v = (v or "").replace("\n", " ").strip()
    if v == "" or re.search(r'[:#\-\[\]{}",\']', v) or v[:1].isdigit():
        return '"' + v.replace('"', '\\"') + '"'
    return v


# ---------------------------------------------------------------- projection


def project(d: dict, *, style: str = "structured", include_editorial: bool = False) -> dict:
    """The subset of a stored recipe that is about COOKING, as plain data.

    Why a projection rather than the stored row: a row carries 52 keys and about
    35 of them are machinery — extraction traces, image metadata, authority
    scores, schema.org boilerplate, review arrays. Measured over 20 recipes the
    raw rows are 512 KB (~142k tokens) against 94 KB for this projection. The
    difference is not compression, it is removing things that are not the recipe.

    Both output formats serialise THIS, so `--format json` and `--format md`
    always agree about what a recipe is. The formats differ in shape, never in
    facts.
    """
    src = d.get("_source") or {}
    ident = d.get("_identity") or {}
    cls = d.get("classification") or {}

    o: dict[str, Any] = {"title": _clean(d.get("name")) or "Untitled recipe"}
    if _clean(d.get("description")):
        o["description"] = _clean(d["description"])

    source = {k: v for k, v in (("site", _clean(src.get("siteName")) or _clean(src.get("origin"))),
                                ("url", _clean(src.get("originalUrl")))) if v}
    if source:
        o["source"] = source

    times = {k: v for k, v in (("prep", _iso_duration_to_human(d.get("prepTime"))),
                               ("cook", _iso_duration_to_human(d.get("cookTime"))),
                               ("total", _iso_duration_to_human(d.get("totalTime")))) if v}
    if times:
        o["times"] = times
    if _clean(d.get("recipeYield")):
        o["yield"] = _clean(d["recipeYield"])

    ings = [_clean(i) for i in (d.get("recipeIngredient") or []) if _clean(i)]
    roles = ident.get("ingredientRoles") or []
    aligned = (style == "structured" and len(roles) == len(ings)
               and all(isinstance(r, dict) for r in roles))
    if aligned:
        # Roles are positionally aligned with recipeIngredient by the extractor.
        # Only pair them when the lengths agree — a mismatch must degrade to a
        # plain list rather than mislabel an ingredient.
        o["ingredients"] = [{"text": t, "role": roles[i].get("role")} if roles[i].get("role")
                            else {"text": t} for i, t in enumerate(ings)]
    elif ings:
        o["ingredients"] = ings

    steps = [s for s in (_text_of(s) for s in (d.get("recipeInstructions") or [])) if s]
    if steps:
        o["method"] = steps          # plain strings; the HowToStep wrapper is overhead
    if _clean(d.get("notes")):
        o["notes"] = _clean(d["notes"])

    if style == "structured":
        for key, val in (("cuisine", _clean(ident.get("cuisine")) or _clean(d.get("recipeCuisine"))),
                         ("technique", _clean(ident.get("technique"))),
                         ("chapter", _clean(cls.get("chapter")))):
            if val:
                o[key] = val
        equip = []
        for e in (d.get("equipment") or []):
            if isinstance(e, dict) and _clean(e.get("name")):
                item = {"name": _clean(e["name"])}
                if _clean(e.get("size")):
                    item["size"] = _clean(e["size"])
                equip.append(item)
            elif isinstance(e, str) and e.strip():
                equip.append({"name": e.strip()})
        if equip:
            o["equipment"] = equip
        prov = {k: v for k, v in (d.get("provenance") or {}).items()
                if v not in (None, "", [], {})}
        prov.pop("sources", None)
        if prov:
            o["provenance"] = prov

    if include_editorial and _clean((d.get("editorial") or {}).get("opinion")):
        o["editorial"] = _clean(d["editorial"]["opinion"])
    return o


# ---------------------------------------------------------------- rendering


def render(d: dict, *, style: str = "structured", include_editorial: bool = False) -> str:
    out: list[str] = []
    name = _clean(d.get("name")) or "Untitled recipe"
    src = d.get("_source") or {}
    ident = d.get("_identity") or {}
    url = _clean(src.get("originalUrl"))
    site = _clean(src.get("siteName")) or _clean(src.get("origin"))

    prep = _iso_duration_to_human(d.get("prepTime"))
    cook = _iso_duration_to_human(d.get("cookTime"))
    total = _iso_duration_to_human(d.get("totalTime"))
    yld = _clean(d.get("recipeYield"))
    cuisine = _clean(ident.get("cuisine")) or _clean(d.get("recipeCuisine"))
    technique = _clean(ident.get("technique"))
    chapter = _clean((d.get("classification") or {}).get("chapter"))

    # --- YAML front matter: what a notebook can filter and group on ---------
    if style == "structured":
        out.append("---")
        out.append(f"title: {_yaml_scalar(name)}")
        if site:
            out.append(f"source: {_yaml_scalar(site)}")
        if url:
            out.append(f"url: {_yaml_scalar(url)}")
        if cuisine:
            out.append(f"cuisine: {_yaml_scalar(cuisine)}")
        if chapter:
            out.append(f"chapter: {_yaml_scalar(chapter)}")
        if technique:
            out.append(f"technique: {_yaml_scalar(technique)}")
        if total:
            out.append(f"total_time: {_yaml_scalar(total)}")
        if yld:
            out.append(f"yield: {_yaml_scalar(yld)}")
        roles = [r.get("role") for r in (ident.get("ingredientRoles") or [])
                 if isinstance(r, dict) and r.get("role")]
        if roles:
            out.append("ingredient_roles: [" + ", ".join(sorted(set(roles))) + "]")
        out.append("---")
        out.append("")

    out.append(f"# {name}")
    out.append("")

    desc = _clean(d.get("description"))
    if desc:
        out.append(desc)
        out.append("")

    # --- the facts line ----------------------------------------------------
    facts = []
    if yld:
        facts.append(f"**Yield:** {yld}")
    if prep:
        facts.append(f"**Prep:** {prep}")
    if cook:
        facts.append(f"**Cook:** {cook}")
    if total:
        facts.append(f"**Total:** {total}")
    if facts:
        out.append(" · ".join(facts))
        out.append("")

    # --- ingredients -------------------------------------------------------
    ings = [i for i in (d.get("recipeIngredient") or []) if _clean(i)]
    if ings:
        out.append("## Ingredients")
        out.append("")
        # In structured mode, annotate each line with the role we parsed. The
        # roles list is positionally aligned with recipeIngredient by the
        # extractor, so zip is correct — but guard the length, because a
        # mismatch should degrade to a plain list, never mislabel an ingredient.
        roles = ident.get("ingredientRoles") or []
        aligned = (style == "structured"
                   and len(roles) == len(ings)
                   and all(isinstance(r, dict) for r in roles))
        for idx, ing in enumerate(ings):
            if aligned and roles[idx].get("role"):
                out.append(f"- {_clean(ing)}  *[{roles[idx]['role']}]*")
            else:
                out.append(f"- {_clean(ing)}")
        out.append("")

    # --- equipment ---------------------------------------------------------
    if style == "structured":
        equip = []
        for e in (d.get("equipment") or []):
            if isinstance(e, dict) and _clean(e.get("name")):
                size = _clean(e.get("size"))
                equip.append(f"{_clean(e['name'])}" + (f" ({size})" if size else ""))
            elif isinstance(e, str) and e.strip():
                equip.append(e.strip())
        if equip:
            out.append("## Equipment")
            out.append("")
            for e in equip:
                out.append(f"- {e}")
            out.append("")

    # --- method ------------------------------------------------------------
    steps = [_text_of(s) for s in (d.get("recipeInstructions") or [])]
    steps = [s for s in steps if s]
    if steps:
        out.append("## Method")
        out.append("")
        for n, s in enumerate(steps, 1):
            out.append(f"{n}. {s}")
        out.append("")

    notes = _clean(d.get("notes"))
    if notes:
        out.append("## Notes")
        out.append("")
        out.append(notes)
        out.append("")

    # --- provenance: the part a general tool has no way to know -------------
    if style == "structured":
        prov = d.get("provenance") or {}
        bits = []
        for label, key in (("Origin", "originRegion"), ("Ethnicity", "ethnicity"),
                           ("Context", "traditionalContext")):
            if _clean(prov.get(key)):
                bits.append(f"- **{label}:** {_clean(prov[key])}")
        variations = [v for v in (prov.get("notableVariations") or []) if _clean(v)]
        if variations:
            bits.append("- **Variations:** " + "; ".join(variations))
        related = [v for v in (prov.get("relatedDishes") or []) if _clean(v)]
        if related:
            bits.append("- **Related dishes:** " + ", ".join(related))
        if bits:
            out.append("## Provenance")
            out.append("")
            out.extend(bits)
            out.append("")

    if include_editorial:
        ed = (d.get("editorial") or {}).get("opinion")
        if _clean(ed):
            out.append("## Editorial note")
            out.append("")
            out.append(_clean(ed))
            out.append("")

    # --- attribution, always ----------------------------------------------
    if url:
        out.append("---")
        out.append("")
        out.append(f"Source: [{site or url}]({url})")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- data access


def fetch(db: str, *, master: bool, user_id: Optional[int],
          ids: Optional[list[str]], limit: int) -> list[tuple[str, dict]]:
    table = "master_recipes" if master else "recipes"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sql = f"SELECT recipe_id, data FROM {table}"
        params: list[Any] = []
        where = []
        if not master and user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if ids:
            # Prefix match so short ids from a URL work without the full UUID.
            where.append("(" + " OR ".join(["recipe_id LIKE ?"] * len(ids)) + ")")
            params.extend(f"{i}%" for i in ids)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = []
        for rid, raw in conn.execute(sql, params):
            try:
                rows.append((rid, json.loads(raw)))
            except Exception as e:
                print(f"  ! skipped {rid}: unreadable JSON ({e})", file=sys.stderr)
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Export saved recipes as Markdown.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--master", action="store_true",
                     help="Export from the curated master library (user 0).")
    src.add_argument("--user", type=int, help="Export one user's own recipes.")
    ap.add_argument("--ids", help="Comma-separated recipe ids (prefixes are fine).")
    ap.add_argument("--limit", type=int, default=20,
                    help="Max recipes to export (default 20 — the free-tier ceiling).")
    ap.add_argument("--out", default="temp/recipe-md",
                    help="Output directory (default temp/recipe-md).")
    ap.add_argument("--format", choices=("md", "json"), default="md",
                    help="md for document tools (NotebookLM and friends read .md "
                         "natively); json for feeding a model directly, where arrays "
                         "stay addressable instead of being re-parsed from bullets.")
    ap.add_argument("--style", choices=("structured", "plain"), default="structured",
                    help="structured keeps roles/equipment/provenance; plain is an "
                         "ordinary recipe, for comparing whether structure helps.")
    ap.add_argument("--include-editorial", action="store_true",
                    help="Include our editorial opinion (off by default).")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    if not args.master and args.user is None:
        ap.error("choose a source: --master or --user N")
    if not os.path.exists(args.db):
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2

    ids = [i.strip() for i in args.ids.split(",")] if args.ids else None
    rows = fetch(args.db, master=args.master, user_id=args.user, ids=ids, limit=args.limit)
    if not rows:
        print("nothing matched.", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    ext = ".json" if args.format == "json" else ".md"
    written, bundle = [], []
    for rid, d in rows:
        stem = f"{_slug(d.get('name'))}-{(rid or '')[:8]}"
        path = os.path.join(args.out, stem + ext)
        proj = project(d, style=args.style, include_editorial=args.include_editorial)
        with open(path, "w", encoding="utf-8") as fh:
            if args.format == "json":
                json.dump(proj, fh, ensure_ascii=False, indent=2)
                bundle.append(proj)
            else:
                fh.write(render(d, style=args.style,
                                include_editorial=args.include_editorial))
        written.append((stem + ext, _clean(d.get("name")) or "Untitled"))
        _say(f"  {path}")

    # One file holding everything, because a synthesis prompt wants the whole
    # library in a single payload — not twenty attachments to reassemble.
    if args.format == "json":
        bpath = os.path.join(args.out, "000-library.json")
        with open(bpath, "w", encoding="utf-8") as fh:
            json.dump({"recipes": bundle, "count": len(bundle), "style": args.style},
                      fh, ensure_ascii=False, indent=2)
        _say(f"  {bpath}   <- the one to paste")

    # An index, so a notebook has one place to see what it was given.
    index = os.path.join(args.out, "000-index.md")
    who = "curated master library" if args.master else f"user {args.user}"
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(f"# Recipe export — {who}\n\n")
        fh.write(f"{len(written)} recipes, `{args.style}` style, "
                 f"exported {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.\n\n")
        for fname, title in written:
            fh.write(f"- [{title}]({fname})\n")
    print(f"\n{len(written)} recipes -> {args.out}  (+ 000-index.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
