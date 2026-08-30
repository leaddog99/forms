"""Which canonical dish is this recipe? — one implementation, three callers.

The rule is **infer a dish when the row does not already know one**. It is NOT
"user rows get a match and master rows don't", which is what the save path used
to do: master rows were assumed to carry `_master.dish` because a dish refresh
curates them FOR a dish, and that is true of harvested rows and false of every
interactive capture. Those rows stored a vector and discarded the one thing the
vector was for.

Three callers share this: the save path (`save_recipe_api._stamp_dish_match`),
the backfill script, and the nightly `dish_rematch` job. They were going to
drift — the save path and the first backfill already disagreed about whether to
stamp the vec index — so the logic lives here once.

WHY A SWEEP EXISTS AT ALL. New recipes are matched AT SAVE, so a sweep is not
how new recipes get a dish. What the sweep is for is the other direction: the
DISH CATALOG changes (~45-60 new dishes a month, plus description/query edits
that move a dish's own vector), and a row already carries the best match from a
catalog that no longer exists. Creating "Pumpkin Pie" does not move the pumpkin
pies out of Cream Pie; only a re-score does.

WRITE ONLY ON CHANGE. The sweep re-scores every unclaimed row but writes only
the ones whose verdict actually moved. This matters because it runs on a
schedule: stamping a fresh `matched_at` on every row every night would dirty
thousands of JSON blobs for nothing, inflate the WAL, and make every night's
`recipes.sql` backup diff the size of the table.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from input.pipeline import vector_store
from input.pipeline.embeddings import bytes_to_vec

DEFAULT_MAX_DIST = 0.6
_SETTING = "dish_match_max_distance"


def max_distance(conn_db_path: Optional[str] = None) -> float:
    """The confidence threshold, from system_config.

    Lowered 0.8 -> 0.6 on 2026-08-21 against measured data: agreement between
    the match and the recipe's own identity-card `likelyDish` fell off a cliff
    above ~0.6 (0.5-0.6 band 29% disagreement, 0.6-0.7 65%, 0.75-0.8 85%), and
    the 0.75-0.8 band was returning things like Pumpkin Spice Latte -> Pumpkin
    Pie. Dropping to 0.6 took the overall disagreement rate from 45% to 14%.
    """
    try:
        from input.pipeline import system_config as _cfg
        # db_path passed through EXPLICITLY where the caller knows it:
        # get_setting() with no db_path lazily imports save_recipe_api to read
        # DB_PATH, and that import runs the app's startup, which resets
        # in-flight jobs.
        if conn_db_path:
            return float(_cfg.get_setting(_SETTING, DEFAULT_MAX_DIST,
                                          db_path=conn_db_path))
        return float(_cfg.get_setting(_SETTING, DEFAULT_MAX_DIST))
    except Exception:
        return DEFAULT_MAX_DIST


def _fold_name(t: str) -> str:
    """Accent-fold + lowercase, the same treatment the coverage page uses —
    Tiramisù and Tiramisu are one name."""
    d = unicodedata.normalize("NFD", t or "")
    return "".join(ch for ch in d if not unicodedata.combining(ch)).strip().lower()


def name_index(conn: sqlite3.Connection) -> dict:
    """folded name -> canonical dish name, over dishes.name + display_name +
    each entry of the aliases JSON array. Exact folded equality is the ONLY
    lookup this supports, by design: token-subset matching was measured on
    2026-08-23 and mis-filed Boston Cream Pie under Cream Pie and a Greek
    pasta salad under Greek Salad. 190-ish rows — cheap to rebuild per call;
    sweeps pass one in to avoid the re-query."""
    idx: dict = {}
    for name, display, aliases in conn.execute(
            "SELECT name, display_name, aliases FROM dishes"):
        for form in (name, display):
            f = _fold_name(form or "")
            if f:
                idx.setdefault(f, name)
        try:
            for a in json.loads(aliases or "[]"):
                f = _fold_name(str(a))
                if f:
                    idx.setdefault(f, name)
        except (ValueError, TypeError):
            pass
    return idx


# ── Qualifier families — "Base - Qualifier" siblings ────────────────────────
# The catalog convention "Chicken Breast - Bone-In" / "Chicken Breast -
# Boneless" names near-identical dishes split on one explicit attribute.
# Embeddings CANNOT separate them: 95% of the ingredient text is shared, so
# the sibling margins are noise (measured 2026-08-30: Stuffed Chicken Breast
# sat 0.8794 vs 0.8796 between the two breast siblings). The attribute is a
# TOKEN, and the token is right there in the recipe's own words — so, like the
# name-exact override above, explicit text evidence beats the vector.

def _fold_text(t: str) -> str:
    """Lowercase, every non-alphanumeric run -> single space, padded — so
    'Bone-In' and 'bone in' compare equal and matches are token-bounded."""
    return " " + re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip() + " "


def qualifier_families(catalog_names: Iterable[str]) -> dict:
    """{dish_name: (base, folded_qualifier)} for every catalog dish that is a
    member of a qualifier family: names following 'Base - Qualifier' where at
    least TWO dishes share the base. A lone 'Pasta & Noodles - Agnolotti' is
    not a family — there is no sibling to disambiguate against."""
    by_base: dict = defaultdict(dict)
    for n in set(catalog_names):
        if " - " in n:
            base, qual = n.rsplit(" - ", 1)
            by_base[base.strip().lower()][n] = _fold_text(qual)
    return {n: (b, q) for b, sibs in by_base.items() if len(sibs) > 1
            for n, q in sibs.items()}


def evidence_text(recipe_dict: dict) -> str:
    """The recipe's own words a qualifier can be evidenced from: title +
    ingredient lines. The title is EXCLUDED from the embedding on purpose
    (marketing spread), but for a literal token check it is the single best
    source — 'Baked Bone In Chicken Breast' says the attribute outright."""
    d = recipe_dict or {}
    return " ".join([str(d.get("name") or "")]
                    + [str(x) for x in (d.get("recipeIngredient") or [])])


def _evidenced_sibling(dish_name: str, text: str, fam: dict) -> Optional[str]:
    """Which member of `dish_name`'s family does `text` explicitly evidence?
    Returns that single member, or None when dish_name is not in a family, no
    qualifier appears, or MORE than one does ('boneless or bone-in' — the
    text is hedging, not claiming)."""
    if dish_name not in fam or not text:
        return None
    base = fam[dish_name][0]
    ftext = _fold_text(text)
    hits = [n for n, (b, q) in fam.items() if b == base and q in ftext]
    return hits[0] if len(hits) == 1 else None


def qualifier_contradiction(dish_name: str, text: str,
                            catalog_names: Iterable[str]) -> Optional[str]:
    """If `dish_name` belongs to a qualifier family and the recipe's text
    explicitly evidences exactly ONE — different — sibling, return that
    sibling; else None. The dish-refresh guard: a run harvesting Boneless
    must not stamp a recipe whose own words say bone-in."""
    sib = _evidenced_sibling(dish_name, text, qualifier_families(catalog_names))
    return sib if (sib and sib != dish_name) else None


def build_match(conn: sqlite3.Connection, rec_vec, *, max_dist: float,
                likely_dish: str = "",
                names: Optional[dict] = None,
                text: str = "") -> Optional[dict]:
    """The `_match` block for a recipe vector, or None if the dish index is
    empty. Reuses the vector the caller already has — no embed, no API call.

    NAME EVIDENCE OVERRIDES DISTANCE (2026-08-23). When the identity card's
    `likelyDish` IS a catalog dish — exact after folding, via name/display/
    alias — that is a literal identity claim and it wins over the embedding
    verdict in BOTH directions: it claims a row the distance bar would
    strand (a dozen plain lasagnas sat at 0.60-0.69, unassigned, while
    likelyDish said "Lasagna"), and it corrects a confident-but-wrong
    neighbour (the same akispetretzikis lasagna's nearest dish was
    Bolognese, the sauce). The distance and candidates are still recorded —
    the override hides nothing, and the two disagreeing stays visible."""
    cands = vector_store.find_similar_dishes(conn, rec_vec, k=3)
    if not cands:
        return None
    names_idx = names if names is not None else name_index(conn)
    best = cands[0]
    confident = best["distance"] <= max_dist
    dish = best["name"] if confident else None
    method = None
    hit = names_idx.get(_fold_name(likely_dish)) if likely_dish else None
    if hit and hit != dish:
        dish, confident, method = hit, True, "name-exact"
    # QUALIFIER EVIDENCE REORDERS SIBLINGS (2026-08-30). When the nearest dish
    # is a member of a qualifier family and the recipe's own text evidences
    # exactly one sibling, that sibling takes the head of the candidate list —
    # the sibling margins are embedding noise, the token is a literal claim.
    # Confidence is still the distance bar (unlike name-exact, the qualifier
    # names an ATTRIBUTE, not the dish — a bone-in pork chop also says
    # 'bone in'); an unconfident row still fixes its NEAREST rung. Never
    # applied over a name-exact hit, and never pulls in a dish that wasn't
    # already a candidate.
    if text and method is None:
        fam = qualifier_families(set(names_idx.values()))
        sib = _evidenced_sibling(best["name"], text, fam)
        if sib and sib != best["name"]:
            tc = next((c for c in cands if c["name"] == sib), None)
            if tc is not None:
                cands.remove(tc)
                cands.insert(0, tc)
                best = tc
                confident = best["distance"] <= max_dist
                dish = best["name"] if confident else None
                method = "qualifier"
    out = {
        "dish": dish,
        "distance": round(best["distance"], 4),
        "confident": confident,
        "candidates": [
            {"dish": m["name"], "distance": round(m["distance"], 4)}
            for m in cands
        ],
        "matched_at": datetime.now(timezone.utc).isoformat(),
    }
    if method:
        out["method"] = method
    return out


def same_verdict(old: Optional[dict], new: Optional[dict]) -> bool:
    """Do two `_match` blocks say the same thing? `matched_at` is excluded on
    purpose — it changes every run and is not part of the verdict, so including
    it would make every row look changed and defeat write-on-change."""
    if not old or not new:
        return False
    return (old.get("dish") == new.get("dish")
            and bool(old.get("confident")) == bool(new.get("confident"))
            and old.get("distance") == new.get("distance"))


def rematch_unclaimed(conn: sqlite3.Connection, *, db_path: Optional[str] = None,
                      table: str = "master_recipes",
                      limit: int = 0, dry_run: bool = False,
                      only_unmatched: bool = False,
                      log=print) -> dict:
    """Re-score every row (of `table`) that carries no curated `_master.dish`.

    `table` — master_recipes (default) or recipes: the PERSONAL collections
    resolve a dish through the same ladder (docs/dish-product-matching.md), so
    the sweep covers both; user rows differ only in having no vec-index row to
    keep in step (recipes_master_vec is master-only by design — user rows are
    matched AGAINST dishes_vec, never KNN targets themselves).

    `only_unmatched=True` restricts to rows that have never been matched (the
    first-pass backfill). The default re-scores rows that already have a match,
    which is the point on a schedule — the catalog moved under them.

    Returns a summary dict; writes only rows whose verdict changed.
    """
    assert table in ("master_recipes", "recipes")
    max_dist = max_distance(db_path)
    vector_store.enable_vec(conn)

    sql = (f"SELECT id, data, embedding FROM {table} "
           " WHERE embedding IS NOT NULL "
           "   AND json_extract(data, '$._master.dish') IS NULL")
    if only_unmatched:
        sql += "   AND json_extract(data, '$._match') IS NULL"
    sql += " ORDER BY id"

    rows = conn.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]

    scanned = confident = changed = failed = 0
    moves: list[tuple] = []

    names = name_index(conn)
    for n, (rid, dj, blob) in enumerate(rows, 1):
        try:
            d = json.loads(dj)
            prev = d.get("_match") or None
            likely = ((d.get("_identity") or {}).get("likelyDish") or "")
            new = build_match(conn, bytes_to_vec(blob), max_dist=max_dist,
                              likely_dish=likely, names=names,
                              text=evidence_text(d))
            scanned += 1
            if new is None:
                continue
            if new["confident"]:
                confident += 1
            if same_verdict(prev, new):
                continue                      # <- the whole point: no write

            changed += 1
            # BOTH matched-dish fields, before -> after (curator request
            # 2026-08-29): the confident verdict (_match.dish, rung 2 of the
            # resolution ladder) AND the nearest candidate (candidates[0],
            # rung 3 — what dish_effective falls back to). Either can move
            # independently: a catalog change can flip the nearest without
            # crossing the confidence bar, and that change is invisible in a
            # verdict-only log while still re-aiming gear inheritance.
            _p_near = (((prev or {}).get("candidates") or [{}])[0]).get("dish")
            _n_near = ((new.get("candidates") or [{}])[0]).get("dish")
            moves.append((rid, (prev or {}).get("dish"), new["dish"], _p_near, _n_near))
            if dry_run:
                continue

            d["_match"] = new
            conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                         (json.dumps(d), rid))
            # Unconditional, dish=None when not confident: a row DEMOTED from a
            # confident match would otherwise keep the stale dish in the index
            # while `data` said otherwise, and the KNN filter reads the index.
            # Master only — user rows have no vec-index row (see docstring).
            if table == "master_recipes":
                ch = ((d.get("classification") or {}).get("chapter") or None)
                vector_store.upsert_recipe_vector(
                    conn, rid, bytes_to_vec(blob), chapter=ch, dish=new["dish"])
            if changed % 200 == 0:
                conn.commit()
        except Exception as e:
            failed += 1
            log(f"[REMATCH] row {rid} FAILED: {type(e).__name__}: {e}")

    if not dry_run:
        conn.commit()

    return {
        "scanned": scanned,
        "confident": confident,
        "changed": changed,
        "unchanged": scanned - changed,
        "failed": failed,
        "threshold": max_dist,
        "moves": moves,
    }
