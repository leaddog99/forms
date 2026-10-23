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
                text: str = "",
                chapter: str = "") -> Optional[dict]:
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
    # AN UNCONFIDENT GUESS STAYS IN THE RECIPE'S CHAPTER (2026-10-23). The
    # nearest rung is what dish_effective serves when nothing matched, so it
    # decides which cohort an orphan counts in. Sauce Jean Vignard: a chestnut
    # soup (Soups & Stews) had 'Poulet a la Moutarde' (Poultry) as its guess,
    # then the new sauce. When the recipe has a chapter, the guesses come from
    # that chapter; the cross-chapter nearest is kept on `off_chapter` (hides
    # nothing). `distance` stays the overall best - it is the confidence
    # measure. No in-chapter dish at all = the old list, marked. A confident
    # match is never reordered: distance under the bar outranks a chapter label
    # the classifier may have got wrong (the rematch holds a confident
    # cross-chapter MOVE for review instead).
    off_chapter = None
    ch = (chapter or "").strip()
    if not confident and ch and ch != "Uncertain" and _dish_chapter(conn, best["name"]) != ch:
        inside = vector_store.find_similar_dishes(conn, rec_vec, k=3, chapter=ch)
        off_chapter = {"dish": best["name"], "distance": round(best["distance"], 4)}
        if inside:
            cands = inside
        else:
            off_chapter["no_dish_in_chapter"] = True
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
    if off_chapter:
        out["off_chapter"] = off_chapter
    return out


def _dish_chapter(conn: sqlite3.Connection, name: str) -> Optional[str]:
    row = conn.execute("SELECT chapter FROM dishes WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def recipe_chapter(recipe_dict: dict) -> str:
    """The recipe's own chapter (the classifier's), '' when it has none."""
    return str(((recipe_dict.get("classification") or {}).get("chapter")) or "")


def evidence_tier(share: float, median: float, n: int) -> Optional[str]:
    """How strongly do a NAME's recipes say "this dish already exists"?

    Calibrated 2026-09-19 on the 18 dishes created that week, each one's recipes
    voted to their nearest OTHER dish: the seven duplicates scored 82-100% agreement
    at median 0.42-0.56 (Boeuf Bourguignon -> Beef Bourguignon 94% / 0.42; Pasta alla
    Gricia -> Gricia 92% / 0.46); the genuinely new dishes scattered (Galette 16%,
    Poke Bowl 39% / 0.97, Kibbeh 85% but 0.79 away). "same" = a confident majority
    sitting INSIDE the match bar (0.6). "look" = near-unanimous but further out -
    the Mexican Rice -> Arroz shape (100% / 0.74), where the existing dish is vague
    rather than identical. Under 3 recipes is no evidence at all."""
    if n < 3:
        return None
    if share >= 0.6 and median <= 0.6:
        return "same"
    if share >= 0.8 and median <= 0.75:
        return "look"
    return None


def coverage_evidence(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Is `name` already in the catalog under ANOTHER name? Asked of the recipes.

    Names lie: the coverage page compares WORDS, so "Boeuf" vs "Beef", "Pasta al
    Tonno" vs "Spaghetti al Tonno", "Shawarma" vs a misspelt "Schwarma" and "Mexican
    Rice" vs "Arroz" all read as genuine gaps - and seven duplicate dishes were
    created from the holes list in one afternoon (2026-09-19). The recipes do not
    lie: every one already has a vector. Take the recipes whose identity card says
    `name`, send each to its nearest dish OTHER than `name`, and see whether they
    agree. No model call, no embedding call.

    -> {dish, share, median, n, tier} for the best-supported existing dish, or None."""
    try:
        import numpy as np
        rows = conn.execute(
            "SELECT embedding FROM master_recipes WHERE embedding IS NOT NULL AND "
            "(json_extract(data,'$._identity.likelyDish') = ? COLLATE NOCASE "
            " OR dish_effective = ? COLLATE NOCASE)", (name, name)).fetchall()
        if len(rows) < 3:
            return None
        dishes = [(n, bytes_to_vec(e)) for n, e in conn.execute(
            "SELECT name, embedding FROM dishes WHERE embedding IS NOT NULL "
            "AND name != ? COLLATE NOCASE", (name,))]
        if not dishes:
            return None
        mat = np.vstack([np.asarray(v, dtype="float32") for _, v in dishes])
        votes: dict = defaultdict(list)
        for (blob,) in rows:
            v = np.asarray(bytes_to_vec(blob), dtype="float32")
            if v.shape[0] != mat.shape[1]:
                continue
            d = np.linalg.norm(mat - v, axis=1)
            j = int(d.argmin())
            votes[dishes[j][0]].append(float(d[j]))
        if not votes:
            return None
        top = max(votes, key=lambda k: len(votes[k]))
        n = sum(len(x) for x in votes.values())
        share = len(votes[top]) / n
        median = float(np.median(votes[top]))
        return {"dish": top, "share": round(share, 2), "median": round(median, 3), "n": n,
                "tier": evidence_tier(share, median, n)}
    except Exception:
        return None


DEFAULT_OFFDISH_DISTANCE = 1.0
_OFFDISH_SETTING = "dish_offdish_min_distance"
_GENERIC_DISH_WORDS = {"recipe", "recipes", "best", "easy", "the", "and", "with", "for",
                       "style", "fresh", "homemade", "classic"}


def off_dish(conn: sqlite3.Connection, rec_vec, dish_name: str, recipe: dict) -> Optional[str]:
    """Is this recipe a STRANGER to the dish about to claim it? -> a reason, or None.

    A refresh trusted Google's relevance completely, and Google (or the SERP
    vendor) pads a thin query with popular strangers: an easy-kimchi and a smash
    burger were saved as Apple Crumble winners, a vegan pesto as Chicken Cordon
    Bleu, chicken adobo as Chicken Kiev (2026-09-18) - real recipes on strong
    sites, so is-recipe and the authority ranking both wave them through.

    Rejected only when BOTH hold, because either alone is wrong:
      * FAR: distance to the dish >= the setting (1.0). Measured on 4,872 older
        winners: p99 = 0.95; the strangers sat at 1.01-1.21, real galettes 0.70-0.81.
      * NEVER NAMES IT: title + ingredients + keywords + likelyDish carry less
        than most of the distinctive words of the dish's name, display name and
        every alias. Distance alone would evict honest members of ingredient
        dishes - "Dijon Potato Salad" is 1.11 from Dijon and plainly belongs.
    Unmeasurable (no vector, no dish embedding, a name with no ASCII word) -> None:
    when unsure the recipe stays, as before."""
    if rec_vec is None or not dish_name:
        return None
    try:
        import numpy as np
        row = conn.execute("SELECT embedding, display_name, aliases FROM dishes WHERE name = ?",
                           (dish_name,)).fetchone()
        if not row or not row[0]:
            return None
        vec = np.asarray(rec_vec, dtype="float32")
        dvec = np.asarray(bytes_to_vec(row[0]), dtype="float32")
        if dvec.shape != vec.shape:
            return None
        dist = float(np.linalg.norm(vec - dvec))
        limit = DEFAULT_OFFDISH_DISTANCE
        try:
            from input.pipeline import system_config as _cfg
            path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
            if path:
                limit = float(_cfg.get_setting(_OFFDISH_SETTING, limit, db_path=path))
        except Exception:
            pass
        if dist < limit:
            return None
        names = [dish_name, row[1] or ""]
        try:
            al = json.loads(row[2]) if row[2] else []
            names += [a for a in (al if isinstance(al, list) else []) if isinstance(a, str)]
        except Exception:
            pass
        ident = recipe.get("_identity") or {}
        hay = _fold_text(" ".join(str(x) for x in (
            [recipe.get("name") or "", ident.get("likelyDish") or "",
             recipe.get("keywords") or "", recipe.get("recipeCategory") or ""]
            + list(recipe.get("recipeIngredient") or []))))
        squashed = re.sub(r"\s+", "", hay)
        judged = False
        for nm in names:
            stems = list(dict.fromkeys(
                w[:5] for w in re.findall(r"[a-z]{3,}", _fold_text(nm))
                if w not in _GENERIC_DISH_WORDS))
            if not stems:
                continue
            judged = True
            # "Traybake" vs "Breakfast Tray Bake": spacing is not identity.
            if sum((st in hay or st in squashed) for st in stems) * 2 > len(stems):
                return None                      # it names the dish
        if not judged:
            return None
        return f"off-dish: {dist:.2f} from {dish_name!r} and never names it"
    except Exception:
        return None


DEFAULT_MOVE_MARGIN = 0.05
_MOVE_MARGIN_SETTING = "dish_label_move_min_margin"


def move_margin(conn: sqlite3.Connection) -> float:
    """How much CLOSER the challenger dish must be before a label moves.

    Measured 2026-09-18 on the first 50 relabels, graded by a blind judge (title
    + ingredients, no distances): every move with a margin >= 0.11 was right;
    the one clearly wrong move had 0.021 (a Horiatiki recipe pulled to the more
    GENERAL Greek Salad) and a coin-flip had 0.036 (two Strawberries dishes,
    both ~0.9 away - neither a home). Closer-by-a-hair is embedding noise.
    Read through the connection's own file so no app import is triggered."""
    try:
        from input.pipeline import system_config as _cfg
        path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
        if path:
            return float(_cfg.get_setting(_MOVE_MARGIN_SETTING, DEFAULT_MOVE_MARGIN,
                                          db_path=path))
    except Exception:
        pass
    return DEFAULT_MOVE_MARGIN


def label_holder(conn: sqlite3.Connection, rec_vec, current: str, challenger: str,
                 *, likely_dish: str = "", names: Optional[dict] = None) -> dict:
    """Which of two dishes should HOLD a recipe both of them picked?

    A recipe page can win in more than one dish (Cacciatore and Chicken
    Cacciatore, Quiche and Quiche Lorraine). The library row carries ONE dish
    label, and a refresh used to stamp its own name on every winner it saved -
    so the label went to whichever dish refreshed LAST. Measured 2026-09-18 on
    121 shared winners: 120 sat with the most recently refreshed dish; 28 of
    the moves ran general -> specific (intended) and 37 ran specific -> general
    (Chicken Cacciatore emptied by a later Cacciatore refresh).

    Curator's rule: a recipe moves only to the dish it is CLOSER to - the same
    nearest-neighbour judgment the suggested-dish match makes, with the same
    override: a likelyDish that IS one of the two (name / display / alias,
    folded) is a literal identity claim and beats distance. Replayed over the
    shared winners it corrects 41 of 93 labels (a Galaktoboureko under Bougatsa,
    Quiche Lorraine under Quiche, Swedish Meatballs under Meatballs); median
    margin 0.30, so it is not deciding on noise.

    -> {"holder", "method", "current", "challenger"} with the two distances.
    Anything unmeasurable (no vector, a dish with no embedding, a current dish
    that no longer exists) hands the label to the challenger - the prior
    behaviour, so nothing is ever stranded on a dish that cannot be compared."""
    out = {"holder": challenger, "method": "unmeasured", "current": None, "challenger": None}
    if not current or current == challenger:
        out["method"] = "same-dish"
        return out
    names_idx = names if names is not None else name_index(conn)
    hit = names_idx.get(_fold_name(likely_dish)) if likely_dish else None
    if hit in (current, challenger):
        out.update(holder=hit, method="name-exact")
    if rec_vec is None:
        return out
    try:
        import numpy as np
        vec = np.asarray(rec_vec, dtype="float32")
        dist = {}
        for name in (current, challenger):
            row = conn.execute("SELECT embedding FROM dishes WHERE name = ?", (name,)).fetchone()
            blob = row[0] if row else None
            if not blob:
                return out
            dvec = np.asarray(bytes_to_vec(blob), dtype="float32")
            if dvec.shape != vec.shape:
                return out
            dist[name] = round(float(np.linalg.norm(vec - dvec)), 4)
    except Exception:
        return out
    out["current"], out["challenger"] = dist[current], dist[challenger]
    if out["method"] != "name-exact":
        # Clearly closer moves it - by at least move_margin(); closer-by-a-hair
        # and ties stay put (no churn between siblings, no noise moves).
        margin = move_margin(conn)
        out["margin"] = round(dist[current] - dist[challenger], 4)
        out["holder"] = challenger if out["margin"] >= margin else current
        out["method"] = "distance"
    return out


def same_verdict(old: Optional[dict], new: Optional[dict]) -> bool:
    """Do two `_match` blocks say the same thing? `matched_at` is excluded on
    purpose — it changes every run and is not part of the verdict, so including
    it would make every row look changed and defeat write-on-change."""
    if not old or not new:
        return False
    return (old.get("dish") == new.get("dish")
            and bool(old.get("confident")) == bool(new.get("confident"))
            and old.get("distance") == new.get("distance")
            # the nearest rung is what dish_effective serves an orphan; since the
            # in-chapter guess (2026-10-23) it can move while the distance holds
            and _nearest(old) == _nearest(new))


def _nearest(m: Optional[dict]) -> Optional[str]:
    return (((m or {}).get("candidates") or [{}])[0]).get("dish")


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
    flags: list[dict] = []                    # questionable moves, for the staff digest
    held = 0

    names = name_index(conn)
    dinfo = dish_info(conn)
    for n, (rid, dj, blob) in enumerate(rows, 1):
        try:
            d = json.loads(dj)
            prev = d.get("_match") or None
            likely = ((d.get("_identity") or {}).get("likelyDish") or "")
            rch = recipe_chapter(d)
            new = build_match(conn, bytes_to_vec(blob), max_dist=max_dist,
                              likely_dish=likely, names=names,
                              text=evidence_text(d), chapter=rch)
            scanned += 1
            if new is None:
                continue
            if new["confident"]:
                confident += 1
            if same_verdict(prev, new):
                continue                      # <- the whole point: no write

            # A CONFIDENT dish that would change CHAPTER is held, not written
            # (curator 2026-10-23: "a reassignment shouldn't make a chapter
            # migration"). The row keeps its dish; the move goes to the digest
            # for staff to apply by hand or to answer with a dish fix.
            p_dish = (prev or {}).get("dish")
            if (p_dish and new["confident"] and new["dish"] and new["dish"] != p_dish
                    and dinfo.get(p_dish, {}).get("chapter") != dinfo.get(new["dish"], {}).get("chapter")):
                held += 1
                flags.append(_flag(table, rid, d, rch, "match", p_dish, new["dish"], dinfo,
                                   ["held: confident match would change chapter "
                                    f"({dinfo.get(p_dish, {}).get('chapter')} -> "
                                    f"{dinfo.get(new['dish'], {}).get('chapter')})"]))
                continue

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
            f = _questionable(table, rid, d, rch, prev, new, _p_near, _n_near, dinfo)
            if f:
                flags.append(f)
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
        "held": held,
        "threshold": max_dist,
        "moves": moves,
        "flags": flags,
    }


# --- the staff digest: which moves deserve a human look (2026-10-23) ---------
# Sauce Jean Vignard was created at 08:54 with no recipes; by 08:56 the rematch
# had made it the nearest guess of four orphans and a story had been written
# from them. Nothing said so except four log lines. These are the shapes that
# warrant a person, sent as one email after the sweep; a quiet night sends none.

def _cfg_int(key: str, default: int) -> int:
    try:
        from input.pipeline import system_config as _cfg
        return int(_cfg.get_setting(key, default) or default)
    except Exception:
        return default


def dish_info(conn: sqlite3.Connection) -> dict:
    """{dish: {chapter, created_at, members}} in two queries, once per sweep.
    `members` = assigned or confidently matched master rows (dish_story's rule)."""
    from input.pipeline.dish_story import MEMBER_SOURCES
    out = {r[0]: {"chapter": r[1], "created_at": r[2], "members": 0}
           for r in conn.execute("SELECT name, chapter, created_at FROM dishes")}
    ph = ", ".join("?" * len(MEMBER_SOURCES))
    for name, n in conn.execute(
            f"SELECT dish_effective, COUNT(*) FROM master_recipes "
            f"WHERE dish_effective_source IN ({ph}) GROUP BY 1", MEMBER_SOURCES):
        if name in out:
            out[name]["members"] = n
    return out


def _flag(table, rid, d, rch, rung, frm, to, dinfo, reasons) -> dict:
    return {"table": table, "id": rid, "recipe": d.get("name") or "", "recipe_chapter": rch,
            "rung": rung, "from": frm, "to": to,
            "to_chapter": dinfo.get(to, {}).get("chapter"), "reasons": reasons}


def _questionable(table, rid, d, rch, prev, new, p_near, n_near, dinfo) -> Optional[dict]:
    """A written move that deserves a look, or None. Reasons, any of:
    a NEW confident match into another chapter than the recipe's; a guess with
    no dish in the recipe's chapter to fall back on; a move into a dish created
    in the last `rematch_flag_young_days` (7); a move into a dish with fewer than
    `rematch_flag_thin_members` (5) member recipes."""
    p_dish = (prev or {}).get("dish")
    if new["dish"] and new["dish"] != p_dish:
        rung, frm, to = "match", p_dish, new["dish"]
    elif n_near and n_near != p_near:
        rung, frm, to = "nearest", p_near, n_near
    else:
        return None
    reasons = []
    to_ch = dinfo.get(to, {}).get("chapter")
    if rung == "match" and rch and rch != "Uncertain" and to_ch and to_ch != rch:
        reasons.append(f"new match is in {to_ch}, the recipe is {rch}")
    if rung == "nearest" and (new.get("off_chapter") or {}).get("no_dish_in_chapter"):
        reasons.append(f"no {rch} dish to guess, so the guess is {to_ch}")
    created = dinfo.get(to, {}).get("created_at")
    young_days = _cfg_int("rematch_flag_young_days", 7)
    if created:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(created)
            if age.days < young_days:
                reasons.append(f"{to} was created {age.days} day(s) ago")
        except ValueError:
            pass
    members = dinfo.get(to, {}).get("members", 0)
    thin = _cfg_int("rematch_flag_thin_members", 5)
    if members < thin:
        reasons.append(f"{to} has {members} member recipe(s)")
    return _flag(table, rid, d, rch, rung, frm, to, dinfo, reasons) if reasons else None


def flag_digest(flags: list[dict]) -> tuple[str, str]:
    """(subject, body) for the curator email."""
    n_held = sum(1 for f in flags if f["reasons"][0].startswith("held"))
    subject = (f"Dish rematch: {len(flags)} move(s) to check"
               + (f", {n_held} held" if n_held else ""))
    lines = ["The nightly dish rematch made (or, where marked held, refused) these",
             "moves. 'match' = the recipe's confident dish; 'nearest' = the guess an",
             "unmatched recipe counts under.", ""]
    for f in flags:
        lines.append(f"- {f['table']} {f['id']} {f['recipe']!r} ({f['recipe_chapter'] or 'no chapter'}): "
                     f"{f['rung']} {f['from']!r} -> {f['to']!r}")
        lines += [f"    {r}" for r in f["reasons"]]
    return subject, "\n".join(lines)
