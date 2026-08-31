"""Dish -> product-class PROPOSALS — step 3 of docs/dish-product-matching.md.

One LLM call per dish — THE matcher, by decision 2026-08-31 (the mechanical
distance pre-pass was punted after calibration showed it reduces to exact
lookup over curated phrases; scripts/calibrate_class_match.py holds the
evidence). In: the measured cohort_signals (term + lift + example lines),
the dish identity, the class registry WITH each class's trigger-phrase
signals (encoded implications like "egg yolks" -> Egg Separators inform the
model without the cohort writing them), and the METHOD TEXT of a few
representative winners (prep implications — peel/core/strain — live in
instructions the signal miner doesn't read). Out: labeled proposals in
DESCENDING ORDER OF NEED (stamped into `sort`) — each with its matching
PATTERN (identity | implication | passthrough), its derivation ROUTE
(contains | does | from | served_with), a tier, and cited evidence. Every
proposal is snapped against the registry so it lands on an existing class
name or explicitly creates a provisional one — never a fourth spelling.

Nothing proposed here ever renders: rows land status='proposed' in the
dish_product_classes junction, and only curator-approved rows are readable
by any commerce surface. Evidence terms come from the signals; a phrase
quoted from method text is allowed (marked anecdotal — no df/lift) but a
proposal with ONLY anecdotal evidence is capped at tier 3. served_with
stays the world-knowledge exemption, labeled as such.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

MODEL = "claude-sonnet-5"
MAX_TOKENS = 3000

FAMILIES = ("equipment", "gourmet", "travel", "books", "alcohol")
PATTERNS = ("identity", "implication", "passthrough")
ROUTES = ("contains", "does", "from", "served_with")


def ensure_junction(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dish_product_classes (
            dish_name   TEXT NOT NULL,
            class_name  TEXT NOT NULL,
            family      TEXT NOT NULL,
            pattern     TEXT NOT NULL,      -- identity | implication | passthrough
            route       TEXT NOT NULL,      -- contains | does | from | served_with
            tier        INTEGER NOT NULL DEFAULT 3,
            sort        INTEGER,            -- curator's explicit order; NULL = computed
            status      TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | rejected
            rationale   TEXT,
            evidence    TEXT,               -- JSON: signal terms w/ pct+lift that justified it
            new_class   INTEGER NOT NULL DEFAULT 0,  -- 1 = registry row created provisionally
            proposed_at TEXT,
            approved_by TEXT,
            approved_at TEXT,
            PRIMARY KEY (dish_name, class_name)
        )""")
    conn.commit()


def _flatten_instructions(ri) -> list:
    """recipeInstructions -> flat list of step strings (JSON-LD allows plain
    strings, HowToStep dicts, and HowToSection nesting)."""
    out = []
    for x in (ri or []) if isinstance(ri, list) else ([ri] if ri else []):
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
        elif isinstance(x, dict):
            if x.get("text"):
                out.append(str(x["text"]).strip())
            out.extend(_flatten_instructions(x.get("itemListElement")))
    return out


def _winner_methods(conn: sqlite3.Connection, dish_name: str,
                    k: int = 3, cap: int = 1200) -> list:
    """Method text of the dish's k strongest cohort recipes — the implication
    surface (peel/core/strain/pan) the ingredient-line miner doesn't see."""
    rows = conn.execute(
        "SELECT data FROM master_recipes WHERE dish_effective = ? "
        "ORDER BY effective_ou_score DESC LIMIT ?", (dish_name, k)).fetchall()
    out = []
    for (dj,) in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        steps = _flatten_instructions(d.get("recipeInstructions"))
        if steps:
            out.append({"name": (d.get("name") or "?")[:80],
                        "method": " ".join(steps)[:cap]})
    return out


# The prompt TEMPLATE — curator-editable in Settings (system_config key
# `dish_class_propose_prompt`; this constant is only the seed/fallback).
# [[TOKENS]] are substituted at call time; everything else is literal text,
# no escaping rules to trip on.
PROMPT_SEED = """You select PRODUCT CLASSES worth advertising beside recipes for one dish.
A product class is a shoppable category ("Baking Chocolate", "Egg Separators",
"French Cookbooks"), not a specific product.

DISH: [[DISH]] (chapter: [[CHAPTER]])
DESCRIPTION: [[DESCRIPTION]]

MEASURED SIGNALS from the dish's [[COHORT_N]] recipes (lift = how many
times more often the term appears here than across all recipes; <3x = commodity,
10x+ = part of this dish's identity):

INGREDIENTS:
[[INGREDIENT_SIGNALS]]

EQUIPMENT:
[[EQUIPMENT_SIGNALS]]

PROVENANCE: ethnicities=[[ETHNICITIES]] regions=[[REGIONS]]

METHOD TEXT from representative winning recipes (implication evidence the
ingredient signals may miss — peeling, coring, straining, the pan used):
[[METHOD_TEXT]]

EXISTING CLASS REGISTRY (reuse these names VERBATIM when the concept matches;
"triggers" are phrases whose presence in recipe text suggests that class —
use them as association hints):
[[CLASS_REGISTRY]]

Propose 6-12 classes as a JSON array IN DESCENDING ORDER OF NEED — the class
a reader of this dish is most likely to actually buy for comes FIRST. Each item:
{"class_name": str, "family": "equipment|gourmet|travel|books|alcohol",
  "pattern": "identity|implication|passthrough",
  "route": "contains|does|from|served_with",
  "tier": 1|2|3, "rationale": one sentence, "evidence": [signal terms used]}

Rules:
- identity = the signal IS the category (chocolate -> Baking Chocolate). Tier 1
  for the dish's DIFFERENTIATING ingredients, tier 2 for its core gear.
- implication = one reasoning step (egg yolks x6 -> Egg Separators; provenance ->
  cuisine cookbooks). Tier 3 unless overwhelming. Cite the evidence terms.
- passthrough = resolved by a marketplace at render (local cooking classes,
  experiences). Rare; only when provenance clearly supports it.
- route served_with = what you'd SERVE with the finished dish (wine pairing).
  Include AT MOST ONE, family "alcohol", pattern passthrough, tier 3; evidence
  may be empty (world knowledge) but the rationale must name the pairing.
- We only advertise products worth BUYING SPECIALLY: durable kitchen
  equipment, specialty/premium gourmet goods (quality chocolate, spice
  blends, preserves, finishing oils), books, travel, drink pairings. NEVER
  propose supermarket staples a reader gets at any grocery — fresh produce
  (apples, herbs, bananas), leaveners (baking soda/powder), sugars
  (granulated/powdered/brown), flour, eggs, dairy, juices. A dish's
  identity fruit is its IDENTITY, not a product we sell.
- NEVER propose commodities (sugar, butter, salt, water, flour, milk, whisks-
  grade generic gear with lift under 3).
- Evidence terms come from the MEASURED SIGNALS above. A short phrase quoted
  from METHOD TEXT is also allowed WHEN the signals miss the implication
  (e.g. the method says "peel and core the apples" but no signal carries it) —
  but a proposal whose evidence is ONLY method-quoted is anecdotal: tier 3.
- served_with is the one exemption from evidence (world knowledge).
- Precise beats vague: the form data in example lines matters ("Baking
  Chocolate" not "Chocolate" when the lines show bars/chopped).
- STRICT JSON: never put a double-quote character INSIDE a string value.
  Write inch as "in" (9x13 in, never 9x13"), and quote nothing in
  rationales.
Return ONLY the JSON array."""


def _prompt(dish: dict, sig: dict, registry: list, methods: list) -> str:
    def _sig_lines(rows, with_ex):
        out = []
        for r in rows:
            ex = f'  e.g. "{(r.get("examples") or [""])[0]}"' if with_ex and r.get("examples") else ""
            out.append(f'- {r["term"]}: {r["pct"]}% of cohort, lift {r["lift"]}x{ex}')
        return "\n".join(out) or "(none)"
    reg_lines = []
    for n, f, s in registry:
        try:
            trig = [p for p in json.loads(s or "[]") if p and p.strip()][:6]
        except Exception:
            trig = []
        reg_lines.append(f"- {n} [{f}]" + (f" — triggers: {', '.join(trig)}" if trig else ""))
    reg = "\n".join(reg_lines)
    meth = "\n".join(f'--- {m["name"]}\n{m["method"]}' for m in methods) or "(none available)"
    prov = sig.get("provenance") or {}
    try:
        from input.pipeline.system_config import get_setting
        template = str(get_setting("dish_class_propose_prompt", PROMPT_SEED)
                       or PROMPT_SEED)
    except Exception:
        template = PROMPT_SEED
    subs = {
        "[[DISH]]": dish["name"],
        "[[CHAPTER]]": dish.get("chapter") or "?",
        "[[DESCRIPTION]]": (dish.get("description") or "")[:300],
        "[[COHORT_N]]": str(sig.get("cohort_n")),
        "[[INGREDIENT_SIGNALS]]": _sig_lines(sig.get("ingredients") or [], True),
        "[[EQUIPMENT_SIGNALS]]": _sig_lines(sig.get("equipment") or [], False),
        "[[ETHNICITIES]]": str(prov.get("ethnicities") or {}),
        "[[REGIONS]]": str(prov.get("regions") or {}),
        "[[METHOD_TEXT]]": meth,
        "[[CLASS_REGISTRY]]": reg,
    }
    for token, value in subs.items():
        template = template.replace(token, value)
    return template


def propose_for_dish(conn: sqlite3.Connection, dish_name: str) -> dict:
    from intake.products import class_registry as cr
    import llm
    ensure_junction(conn)
    cr.ensure_registry(conn)

    row = conn.execute(
        "SELECT name, chapter, description, cohort_signals FROM dishes WHERE name = ?",
        (dish_name,)).fetchone()
    if row is None:
        raise ValueError(f"dish {dish_name!r} not found")
    if not row[3]:
        raise ValueError(f"dish {dish_name!r} has no cohort_signals — run dish_signals first")
    dish = {"name": row[0], "chapter": row[1], "description": row[2]}
    sig = json.loads(row[3])
    registry = conn.execute(
        "SELECT name, family, signals FROM product_classes ORDER BY name").fetchall()
    methods = _winner_methods(conn, dish_name)

    llm.enter(recipe_id=f"dish:{dish_name}", user_id=0)
    msg = llm.create(operation="dish_class_propose", model=MODEL, max_tokens=MAX_TOKENS,
                     messages=[{"role": "user",
                                "content": _prompt(dish, sig, registry, methods)}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    proposals = _parse_proposals(raw, dish_name)

    now = _now()
    out = []
    for rank, p in enumerate(proposals, 1):
        name = (p.get("class_name") or "").strip()
        family = p.get("family") if p.get("family") in FAMILIES else "equipment"
        pattern = p.get("pattern") if p.get("pattern") in PATTERNS else "implication"
        route = p.get("route") if p.get("route") in ROUTES else "contains"
        tier = int(p.get("tier") or 3)
        if not name:
            continue
        snapres = cr.snap(conn, name)
        new_class = 0
        if snapres["snapped"]:
            name = snapres["name"]           # land on the registered spelling
        else:
            # NO auto-registration (curator 2026-08-31: the generator was
            # minting grocery aisles — classes are the SELLABLE catalog and
            # staff-supplied). The junction row carries the proposed name,
            # flagged new_class; the registry row is created only when the
            # curator APPROVES the chip (set_status). Until then the class
            # doesn't exist and future snaps can't converge on it — that's
            # the point.
            new_class = 1
        # Evidence terms enriched with their measured stats, so the approve
        # chip can say "egg yolks 44%/14x" without re-deriving. A term absent
        # from the signals came from method text: anecdotal (no df/lift), and
        # a proposal with ONLY anecdotal evidence is capped at tier 3.
        stats = {r["term"]: r for r in (sig.get("ingredients") or []) + (sig.get("equipment") or [])}
        evidence = []
        for t in (p.get("evidence") or []):
            e = {"term": t, "pct": stats.get(t, {}).get("pct"),
                 "lift": stats.get(t, {}).get("lift")}
            if e["pct"] is None and e["lift"] is None:
                e["anecdotal"] = True
            evidence.append(e)
        if route != "served_with" and evidence and all(e.get("anecdotal") for e in evidence):
            tier = 3
        existing = conn.execute(
            "SELECT status FROM dish_product_classes WHERE dish_name=? AND class_name=?",
            (dish_name, name)).fetchone()
        if existing and existing[0] in ("approved", "rejected"):
            out.append({"class": name, "skipped": f"already {existing[0]}", "rank": rank})
            continue
        conn.execute(
            "INSERT INTO dish_product_classes(dish_name, class_name, family, pattern, route, "
            "tier, sort, status, rationale, evidence, new_class, proposed_at) "
            "VALUES(?,?,?,?,?,?,?,'proposed',?,?,?,?) "
            "ON CONFLICT(dish_name, class_name) DO UPDATE SET family=excluded.family, "
            "pattern=excluded.pattern, route=excluded.route, tier=excluded.tier, "
            "sort=excluded.sort, rationale=excluded.rationale, evidence=excluded.evidence, "
            "new_class=excluded.new_class, proposed_at=excluded.proposed_at",
            (dish_name, name, family, pattern, route, tier, rank,
             (p.get("rationale") or "").strip(), json.dumps(evidence), new_class, now))
        out.append({"class": name, "family": family, "pattern": pattern,
                    "route": route, "tier": tier, "rank": rank,
                    "new_class": bool(new_class)})
    # A re-propose REPLACES the proposed tier (2026-08-31): stale rows from
    # prior runs kept old evidence numbers and collided sort ranks with the
    # fresh set. Approved/rejected rows are human decisions and never touched
    # — the same delete-and-replace-with-carveout as every other refresh.
    conn.execute(
        "DELETE FROM dish_product_classes WHERE dish_name = ? "
        "AND status = 'proposed' AND (proposed_at IS NULL OR proposed_at < ?)",
        (dish_name, now))
    conn.commit()
    return {"dish": dish_name, "proposals": out,
            "proposed": sum(1 for r in out if "skipped" not in r),
            "new_classes": sum(1 for r in out if r.get("new_class"))}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_for_dish(conn: sqlite3.Connection, dish_name: str) -> list:
    """Every junction row for the dish, evidence decoded, proposed first."""
    ensure_junction(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM dish_product_classes WHERE dish_name = ? "
        "ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, "
        "sort IS NULL, sort, tier, class_name", (dish_name,))]
    # Supply-side join (2026-08-30): each class chip says whether the shop
    # side exists yet — the curated collection built FOR this class, and how
    # many product records carry it. An approved class with neither is the
    # build-me signal; one with a collection links straight to it.
    coll = {r[0]: r[1] for r in conn.execute(
        "SELECT product_class, name FROM curated_collections "
        "WHERE COALESCE(product_class,'') != ''")}
    pcount = {r[0]: r[1] for r in conn.execute(
        "SELECT product_class, COUNT(*) FROM products "
        "WHERE COALESCE(product_class,'') != '' GROUP BY product_class")}
    for r in rows:
        try:
            r["evidence"] = json.loads(r["evidence"] or "[]")
        except Exception:
            r["evidence"] = []
        r["collection"] = coll.get(r["class_name"])
        r["product_count"] = pcount.get(r["class_name"], 0)
    return rows


def set_status(conn: sqlite3.Connection, dish_name: str, class_name: str,
               status: str, who: str = "staff") -> bool:
    """The curator's gate on the money join. approved stamps who/when;
    anything else clears the stamp (a revoked approval must not keep
    claiming one)."""
    if status not in ("proposed", "approved", "rejected"):
        raise ValueError("status must be proposed, approved or rejected")
    ensure_junction(conn)
    approved_by = who if status == "approved" else ""
    approved_at = _now() if status == "approved" else ""
    cur = conn.execute(
        "UPDATE dish_product_classes SET status = ?, approved_by = ?, approved_at = ? "
        "WHERE dish_name = ? AND class_name = ?",
        (status, approved_by, approved_at, dish_name, class_name))
    # Approval IS the staff supply of a new class (2026-08-31): the proposer
    # no longer auto-registers unknown names, so an approved new_class chip
    # creates its registry row here — curator-gated by construction.
    if status == "approved" and cur.rowcount:
        row = conn.execute(
            "SELECT family FROM dish_product_classes WHERE dish_name=? AND class_name=?",
            (dish_name, class_name)).fetchone()
        exists = conn.execute("SELECT 1 FROM product_classes WHERE name=?",
                              (class_name,)).fetchone()
        if not exists:
            from intake.products import class_registry as cr
            now = _now()
            conn.execute(
                "INSERT INTO product_classes(name, category, criteria, buying_guide, "
                "data, family, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (class_name, "", "[]", "",
                 json.dumps({"created_by": "chip-approval", "dish": dish_name}),
                 (row[0] if row else "equipment"), now, now))
            cr.ensure_embeddings(conn)
    conn.commit()
    return cur.rowcount > 0


def delete_rejected(conn: sqlite3.Connection, dish_name: str, class_name: str) -> bool:
    """Hard-delete ONE junction row — REJECTED rows only. A rejection is a
    standing ban (it survives re-proposal), so deleting it means 'forget my
    rejection': the clutter goes, but the next ✨ Propose may seat the class
    again as a fresh proposal. Approved rows must be revoked first; proposed
    rows are rejected, not deleted — the status ladder stays the record."""
    ensure_junction(conn)
    cur = conn.execute(
        "DELETE FROM dish_product_classes "
        "WHERE dish_name = ? AND class_name = ? AND status = 'rejected'",
        (dish_name, class_name))
    conn.commit()
    return cur.rowcount > 0


def set_tier(conn: sqlite3.Connection, dish_name: str, class_name: str, tier: int) -> bool:
    if tier not in (1, 2, 3):
        raise ValueError("tier must be 1, 2 or 3")
    ensure_junction(conn)
    cur = conn.execute(
        "UPDATE dish_product_classes SET tier = ? WHERE dish_name = ? AND class_name = ?",
        (tier, dish_name, class_name))
    conn.commit()
    return cur.rowcount > 0


def _parse_proposals(raw: str, dish_name: str) -> list:
    """Parse the reply's JSON array, surviving the model's favorite crime.

    First sweep attempt 2026-08-29 failed 11 of 25 dishes, every one
    `Expecting ',' delimiter` — unescaped double quotes INSIDE string values,
    overwhelmingly inch marks (`9x13" baking pans`) and quoted phrases in
    rationales. Three layers, cheapest first:
      1. plain json.loads;
      2. mechanical repair — digit+quote becomes `<digit> in`, typographic
         quotes stripped, then a general pass escaping any interior quote
         that isn't followed by a JSON structural character;
      3. one model self-repair call carrying the parse error (costs a
         second cheap call, only on the rare double failure).
    """
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError(f"no JSON array in reply for {dish_name!r}: {raw[:200]}")
    txt = m.group(0)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r'(\d)\s*"', r"\1 in", txt)           # 9x13" -> 9x13 in
    fixed = fixed.replace("“", "'").replace("”", "'")
    try:
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        import llm
        msg = llm.create(operation="dish_class_propose_repair", model="claude-haiku-4-5",
                         max_tokens=MAX_TOKENS, messages=[{"role": "user", "content":
            f"This JSON array is invalid ({e}). Return the SAME content as a "
            f"VALID JSON array — escape or remove any double quotes inside "
            f"string values. Return ONLY the array.\n\n{txt}"}])
        raw2 = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        m2 = re.search(r"\[.*\]", raw2, re.S)
        if not m2:
            raise ValueError(f"repair produced no array for {dish_name!r}") from e
        return json.loads(m2.group(0))
