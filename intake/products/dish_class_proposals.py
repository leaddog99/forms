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
MAX_TOKENS = 14000   # thinking + answer share this budget: 3000 was intermittently
                    # eaten ENTIRELY by thinking (stop=max_tokens, no text) — the
                    # real cause behind 'model reply unusable' and thin samples (2026-09-01)

FAMILIES = ("equipment", "gourmet", "travel", "books", "alcohol")
# Four evidence channels (2026-09-01 four-channel design, replacing
# identity/implication/passthrough — the old values remain valid on stored
# rows): direct = the recipes NAME the class; functional = they name the
# OPERATION it exists for; inferred = the workload implies it (which knife);
# affinity = the DISH ITSELF dictates it (pairing from character, travel/
# books from what the dish IS — not from anything the recipes write).
PATTERNS = ("direct", "functional", "inferred", "affinity",
            "identity", "implication", "passthrough")   # last three: legacy rows
ROUTES = ("contains", "does", "from", "served_with")
# Confidence tier derives from the channel — the channels ARE the evidence
# grades, strongest first.
TIER_BY_PATTERN = {"direct": 1, "functional": 2, "inferred": 2, "affinity": 3,
                   "identity": 1, "implication": 2, "passthrough": 3}


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
PROMPT_SEED = """You analyze ONE dish class and select the PRODUCT CLASSES worth recommending
beside its recipes. A product class is a shoppable category ("Santoku Knife",
"Dutch Ovens (5-6 qt)", "Sauvignon Blanc", "Greek Cookbooks"), not a specific product.

DISH: [[DISH]] (chapter: [[CHAPTER]])
DESCRIPTION: [[DESCRIPTION]]
DISH IDENTITY / PROVENANCE: [[PROVENANCE]]

DISTINCTIVE TERMS (statistically measured across the full [[COHORT_N]]-recipe cohort;
lift = times more frequent here than corpus-wide — context, not a constraint):
[[DISTINCTIVE_TERMS]]

REPRESENTATIVE RECIPES (ingredients + method text — your PRIMARY evidence):
[[RECIPES]]

EXISTING CLASS REGISTRY (reuse these names VERBATIM when the concept matches;
"triggers" are association hints):
[[CLASS_REGISTRY]]

STEP 1 — OPERATIONS. Read the methods and extract the recurring or central cooking
operations as ACTION + OBJECT + IMPORTANT QUALIFIER ("slice beef, thin, across the
grain" — never a bare verb). Also flag any recipe that is clearly a DIFFERENT dish
contaminating the set; exclude its evidence everywhere.

STEP 2 — CLASSES. Propose the product classes with a strong, meaningful, repeated or
central relationship to MAKING or ENJOYING this dish. At most 10; typically 6-8;
never pad with conceivable-but-marginal items (a cutting board is used everywhere —
that is exactly why it is not a recommendation). Order by BUY-LIKELIHOOD: an item the
dish demands that most kitchens LACK outranks a staple most kitchens own; premium
upgrades of owned staples rank below true gaps.

Each class carries its EVIDENCE CHANNEL:
- "direct"     — the recipes NAME the class ("pizza stone", "stand mixer").
- "functional" — the recipes name the OPERATION the class exists for
                 ("knead dough" -> Stand Mixer; "juice the lemons" -> Citrus Juicer).
- "inferred"   — the WORKLOAD implies it: reason from the Step-1 operations to the
                 right class. This is where knife specificity lives: repeated thin
                 slicing / fine mincing -> Santoku or Chef's Knife; carving a rested
                 roast -> Carving Knife; a slit pocket -> Boning/Paring. Never a bare
                 "knife".
- "affinity"   — THE DISH ITSELF dictates it; the recipes need not mention anything.
                 PAIRING: from the dish's character as eaten (rich grilled beef ->
                 Cabernet; citrus-herb seafood -> Sauvignon Blanc). Distinguish
                 alcohol IN the dish (that is a gourmet ingredient class) from
                 alcohol SERVED WITH it. At most one or two pairings.
                 TRAVEL / BOOKS: from what the dish IS — its cuisine and place as
                 culinary fact ("Pastitsio is Greek" earns Greek food tours and Greek
                 cookbooks even if no recipe says so). The geography must be specific
                 and recognizable; a dish that is not really OF anywhere gets NO
                 travel. An affiliate opportunity must NEVER manufacture relevance.

Rules:
- SCOPE each class: "core" (the dish as such needs it) | "variant" (essential only to
  a named variation) | "helpful" (workaround exists — name it).
- COUNT prevalence over the supplied recipes ("k/[[N_RECIPES]]") for direct/functional/
  inferred; count carefully from the text, never estimate. Affinity has no count.
- EVIDENCE: verbatim quoted fragments (with recipe name) for direct/functional/inferred;
  for affinity, one sentence naming the identity or character basis.
- DISAGREEMENT is information: where recipes split on vessel or technique, say so.
- We only recommend products worth BUYING SPECIALLY: durable equipment, specialty or
  premium gourmet goods, books, travel, drink pairings. NEVER supermarket staples —
  fresh produce, leaveners, sugars, flour, eggs, dairy, juices. A dish's identity
  fruit is its identity, not a product.
- Commercial appeal never creates a relationship; it only orders classes that have one.
- Reuse registry names verbatim when the concept truly matches; a genuinely new
  concept gets a natural new name (it becomes a chip pending curator approval).

Return ONLY a JSON object, no prose, double quotes never inside string values:
{"operations": [{"action": "", "object": "", "qualifier": ""}],
 "classes": [{"class_name": "", "family": "equipment|gourmet|travel|books|alcohol",
   "channel": "direct|functional|inferred|affinity",
   "route": "contains|does|from|served_with",
   "scope": "core|variant|helpful", "variant_or_workaround": "",
   "prevalence": "", "reason": "",
   "evidence": [""]}],
 "contaminants": [""], "notes": ""}"""


def _prompt(dish: dict, sig: dict, registry: list, recipes: list) -> str:
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
    # Distinctive terms: context, not constraint (four-channel design) —
    # a compact hint of what the FULL cohort over-uses vs the corpus.
    dist = "; ".join(f"{r['term']} ({r['pct']}%/{r['lift']}x)"
                     for r in (sig.get("ingredients") or [])[:12])
    dist_eq = "; ".join(f"{r['term']} ({r['pct']}%/{r['lift']}x)"
                        for r in (sig.get("equipment") or [])[:8])
    dist = (dist + ("\nEQUIPMENT: " + dist_eq if dist_eq else "")) or "(none)"
    # Provenance merged: the DISH'S OWN identity fields lead (curator
    # 2026-09-01: the dish dictates affinity), cohort tags corroborate.
    cprov = sig.get("provenance") or {}
    prov_bits = []
    if (dish.get("ethnicity") or "").strip():
        prov_bits.append(f"dish ethnicity: {dish['ethnicity'].strip()}")
    if (dish.get("origin_region") or "").strip():
        prov_bits.append(f"dish region: {dish['origin_region'].strip()}")
    if cprov.get("ethnicities"):
        prov_bits.append(f"recipe-tagged ethnicities: {cprov['ethnicities']}")
    if cprov.get("regions"):
        prov_bits.append(f"recipe-tagged regions: {cprov['regions']}")
    prov_line = " · ".join(prov_bits) or "(none stated — rely on what the dish IS)"
    recs = "\n\n".join(
        f"--- {r['name']}" + (f"  [provenance: {r['prov']}]" if r["prov"] else "")
        + f"\nINGREDIENTS: {r['ings']}\nMETHOD: {r['method']}"
        for r in recipes) or "(none available)"
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
        "[[N_RECIPES]]": str(len(recipes)),
        "[[DISTINCTIVE_TERMS]]": dist,
        "[[PROVENANCE]]": prov_line,
        "[[RECIPES]]": recs,
        "[[CLASS_REGISTRY]]": reg,
    }
    for token, value in subs.items():
        template = template.replace(token, value)
    return template


def _gather_recipes(conn: sqlite3.Connection, dish_name: str,
                    k: int = 15, cap: int = 1600) -> list:
    """The k strongest cohort recipes with ingredients + method + their own
    provenance tags — the call's primary evidence.

    Rung before OU (2026-09-01, Beef and Broccoli: 51 ungated nearest-rung
    strays from big sites outranked the dish's own curated winners and only
    2 core recipes made the sample): assigned winners first, then confident
    matches, nearest last — OU orders within each rung."""
    rows = conn.execute(
        "SELECT data FROM master_recipes WHERE dish_effective = ? "
        "ORDER BY CASE dish_effective_source WHEN 'assigned' THEN 0 "
        "WHEN 'matched' THEN 1 ELSE 2 END, effective_ou_score DESC LIMIT ?",
        (dish_name, k)).fetchall()
    out = []
    for (dj,) in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        steps = " ".join(_flatten_instructions(d.get("recipeInstructions")))[:cap]
        if not steps:
            continue
        prov = d.get("provenance") or {}
        out.append({
            "name": (d.get("name") or "?")[:70],
            "prov": ", ".join(x for x in (prov.get("ethnicity"), prov.get("originRegion"))
                              if x and str(x).strip()),
            "ings": "; ".join((d.get("recipeIngredient") or [])[:18])[:650],
            "method": steps,
        })
    return out


def _parse_reply(raw: str, dish_name: str) -> dict:
    """The reply's JSON object, surviving the usual quote crimes (same repair
    ladder as the old array parser: plain -> mechanical -> give up loudly)."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"no JSON object in reply for {dish_name!r}: {raw[:200]}")
    txt = m.group(0)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r'(\d)\s*"', r"\1 in", txt)
    fixed = fixed.replace("“", "'").replace("”", "'")
    return json.loads(fixed)


def propose_for_dish(conn: sqlite3.Connection, dish_name: str) -> dict:
    from intake.products import class_registry as cr
    import llm
    ensure_junction(conn)
    cr.ensure_registry(conn)

    row = conn.execute(
        "SELECT name, chapter, description, cohort_signals, ethnicity, origin_region "
        "FROM dishes WHERE name = ?", (dish_name,)).fetchone()
    if row is None:
        raise ValueError(f"dish {dish_name!r} not found")
    if not row[3]:
        raise ValueError(f"dish {dish_name!r} has no cohort_signals — run dish_signals first")
    dish = {"name": row[0], "chapter": row[1], "description": row[2],
            "ethnicity": row[4], "origin_region": row[5]}
    sig = json.loads(row[3])
    registry = conn.execute(
        "SELECT name, family, signals FROM product_classes ORDER BY name").fetchall()
    recipes = _gather_recipes(conn, dish_name)

    llm.enter(recipe_id=f"dish:{dish_name}", user_id=0)
    prompt_text = _prompt(dish, sig, registry, recipes)
    reply = {}
    for attempt in (1, 2):
        msg = llm.create(operation="dish_class_propose", model=MODEL, max_tokens=MAX_TOKENS,
                         messages=[{"role": "user", "content": prompt_text}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        try:
            reply = _parse_reply(raw, dish_name)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"[PROPOSE] {dish_name}: attempt {attempt} unusable ({e}); "
                  f"stop={getattr(msg, 'stop_reason', '?')}")
            reply = {}
        # Thin-sample retry: an empty/near-empty answer on a real dish is a
        # bad SAMPLE (or a thinking-eaten budget), not a verdict.
        if len(reply.get("classes") or []) >= 3:
            break
        if attempt == 1:
            print(f"[PROPOSE] {dish_name}: only "
                  f"{len(reply.get('classes') or [])} class(es) — retrying once")
    proposals = reply.get("classes") or []
    if not proposals:
        raise ValueError(f"model reply unusable for {dish_name!r} — run again")

    # STEP-1 OPERATIONS persist on the dish (value + method + inputs): the
    # audited evidence chain behind the junction rows, and dish data in its
    # own right. Contaminant flags ride along — free cohort hygiene.
    ops_blob = {
        "method": "four-channel-v1", "model": MODEL, "extracted_at": _now(),
        "source_recipes": len(recipes),
        "operations": reply.get("operations") or [],
        "contaminants": reply.get("contaminants") or [],
        "notes": (reply.get("notes") or "")[:600],
    }
    conn.execute("UPDATE dishes SET operations = ? WHERE name = ?",
                 (json.dumps(ops_blob, ensure_ascii=False), dish_name))
    for c in ops_blob["contaminants"]:
        print(f"[PROPOSE] {dish_name}: cohort contaminant flagged — {str(c)[:110]}")

    now = _now()
    out = []
    for rank, p in enumerate(proposals, 1):
        name = (p.get("class_name") or "").strip()
        family = p.get("family") if p.get("family") in FAMILIES else "equipment"
        pattern = (p.get("channel") or p.get("pattern") or "").strip().lower()
        if pattern not in PATTERNS:
            pattern = "inferred"
        route = p.get("route") if p.get("route") in ROUTES else \
            ("served_with" if pattern == "affinity" and family == "alcohol" else
             "from" if pattern == "affinity" else "does")
        tier = TIER_BY_PATTERN.get(pattern, 3)
        scope = (p.get("scope") or "").strip().lower()
        vw = (p.get("variant_or_workaround") or "").strip()
        prevalence = (p.get("prevalence") or "").strip()
        rationale = (p.get("reason") or p.get("rationale") or "").strip()
        tail_bits = [b for b in (
            scope + (f": {vw}" if vw and scope in ("variant", "helpful") else ""),
            prevalence and f"{prevalence} of supplied recipes") if b]
        if tail_bits:
            rationale = f"{rationale} [{' · '.join(tail_bits)}]"
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
        # Evidence is now mostly QUOTED method fragments (the four-channel
        # design); a fragment that happens to be a measured signal term still
        # gets its pct/lift so chips keep their numbers. Quotes are marked
        # anecdotal only in the display sense — the tier comes from the
        # CHANNEL, which already grades the evidence.
        stats = {r["term"]: r for r in (sig.get("ingredients") or []) + (sig.get("equipment") or [])}
        evidence = []
        for t in (p.get("evidence") or []):
            t = str(t).strip()
            if not t:
                continue
            hit = stats.get(t.lower())
            e = {"term": t, "pct": hit.get("pct") if hit else None,
                 "lift": hit.get("lift") if hit else None}
            if not hit:
                e["anecdotal"] = True
            evidence.append(e)
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
             rationale, json.dumps(evidence), new_class, now))
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
