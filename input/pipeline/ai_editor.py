"""ai_editor — the AI reads what a run decided and says where it disagrees.

Phase 1 of docs/ai-editor-mediation.md, in SHADOW: it writes verdicts to
`run_mediations` and changes nothing. Phase 2 gives the verdicts effect.

Why the statistical pass needs an editor at all
-----------------------------------------------
OU measures link-earning exceptionalism, not whether a recipe is the best version
of the dish. Usually those agree. On ramen — the dish with the highest measured
comparison-intent ratio, 272% — they came apart: job 794 ranked a six-ingredient
instant-noodle toss #1 and put Adam Liaw's ramen school, Serious Eats' miso butter
and 101cookbooks' vegan ramen below the floor. Fixing the queries improved it a
lot and did NOT fix this: job 795 still dropped epicurious tonkotsu, doobydobap
and Joshua Weissman's tonkotsu, 35 of 78 in all.

THE EVIDENCE IS ASYMMETRIC, AND IT DECIDES THE VERDICT VOCABULARY
------------------------------------------------------------------
A kept row has been extracted: we hold its ingredients, its method, its times.
A dropped row has not — it was discarded BEFORE extraction, so all we hold is a
title, a URL and three numbers.

That asymmetry is not a detail to paper over with a confident-sounding prompt. It
means the editor can argue convincingly that something kept is WRONG, and cannot
argue convincingly that something dropped is RIGHT. So there is no `promote`
verdict here. Instead:

    hold      the statistical pass got this one right
    demote    a kept recipe that should not be (full evidence -> a real verdict)
    nominate  a dropped URL worth PAYING to fetch and judge (title-level evidence
              -> a proposal, explicitly not a verdict)
    flag      the curator should look; the model will not decide this one

Promoting a page on the strength of its title would be exactly the failure this
project keeps writing down: a mechanism asserted without measurement. `nominate`
makes the cost of being sure explicit, and Phase 2 can spend it.

Facts are not on the table
--------------------------
The editor only ever sees `mediatable_for_run`, which excludes drops that were
observations rather than inferences (the curator's blocklist, an archive page, a
roundup). That boundary lives in SQL, not in the prompt, because a boundary that
lives in a prompt is one that erodes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import llm
import cook_costs
from input.pipeline import candidate_ledger

# Judgment-heavy work: this exists precisely to do what the arithmetic could not.
# Same tier cook_rework uses for its authoring pass.
MODEL = "claude-opus-4-8"
PROMPT_VERSION = "ai-editor-1"
# A full review is ~20 verdicts x (five axes + cited evidence + rationale), and 8000
# truncated on the very first real run against a 20-recipe dish. Sized from that
# measurement with headroom, not from a guess — the failure mode is a forced tool_use
# returning partial JSON whose missing tail reads as "no verdicts".
_MAX_TOKENS = 20000

# How much of a kept recipe to show. Full JSON for 20 recipes would be ~40k tokens
# of mostly boilerplate; the model needs the SHAPE of the method, not every word.
_MAX_INGREDIENTS = 40
_MAX_STEPS = 24
_STEP_CHARS = 260


VERDICTS = ("hold", "demote", "nominate", "flag")


def ensure_mediation_table(conn: sqlite3.Connection) -> None:
    """One row per (run, url) the editor spoke about.

    `applied` is the shadow switch and the audit trail at once: 0 means the verdict
    was recorded and changed nothing. The human_* columns are how calibration gets
    measured later — agreement rate is what should decide whether this ever earns
    authority, and it cannot be computed if the disagreements are not stored.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_mediations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id           INTEGER NOT NULL,
            collection_type  TEXT NOT NULL,
            collection_key   TEXT NOT NULL COLLATE NOCASE,
            url              TEXT NOT NULL,
            url_normalized   TEXT NOT NULL,
            verdict          TEXT NOT NULL,
            ordinal_rank     INTEGER,           -- the editor's own ordering of the kept set
            band             REAL,              -- 3.0..5.0, monotonic with ordinal_rank
            axes             TEXT,              -- JSON: the five rubric axes
            evidence         TEXT,              -- facts cited FROM the record
            rationale        TEXT,
            actor            TEXT NOT NULL DEFAULT 'ai',
            model            TEXT,
            prompt_version   TEXT,
            cost_usd         REAL,
            applied          INTEGER NOT NULL DEFAULT 0,   -- shadow => 0
            human_verdict    TEXT,
            human_note       TEXT,
            human_at         TEXT,
            created_at       TEXT NOT NULL,
            UNIQUE(job_id, url_normalized, actor)
        )
    """)
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_run_mediations_job ON run_mediations(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_run_mediations_coll "
        "ON run_mediations(collection_type, collection_key)",
        "CREATE INDEX IF NOT EXISTS idx_run_mediations_verdict ON run_mediations(verdict)",
    ):
        conn.execute(ddl)
    conn.commit()


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
def _kept_evidence(conn: sqlite3.Connection, url_normalized: str) -> dict:
    """What we actually hold about a kept recipe. Trimmed, not summarized — the
    model must be able to CITE from this, so every field is verbatim source data."""
    row = conn.execute(
        "SELECT data FROM master_recipes WHERE url_normalized = ? LIMIT 1",
        (url_normalized,)).fetchone()
    if not row:
        return {}
    try:
        d = json.loads(row[0])
    except Exception:
        return {}
    ings = [str(i) for i in (d.get("recipeIngredient") or [])][:_MAX_INGREDIENTS]
    steps_raw = d.get("recipeInstructions") or []
    steps = []
    for s in steps_raw[:_MAX_STEPS]:
        if isinstance(s, dict):
            s = s.get("text") or s.get("name") or ""
        steps.append(str(s)[:_STEP_CHARS])
    return {
        "name": d.get("name"),
        "ingredient_count": len(d.get("recipeIngredient") or []),
        "ingredients": ings,
        "step_count": len(steps_raw),
        "steps": steps,
        "totalTime": d.get("totalTime"),
        "equipment": [e.get("name") if isinstance(e, dict) else str(e)
                      for e in (d.get("equipment") or [])][:15],
        "cuisine": d.get("recipeCuisine"),
    }


def build_packet(conn: sqlite3.Connection, job_id: int) -> dict:
    """The mediation packet: the run, its winners with evidence, and the drops the
    editor is allowed to argue about."""
    conn.row_factory = sqlite3.Row
    mt = candidate_ledger.mediatable_for_run(conn, job_id)
    if not mt["kept"] and not mt["reconsider"]:
        return {}
    first = (mt["kept"] or mt["reconsider"])[0]
    ctype, ckey = first["collection_type"], first["collection_key"]

    identity = None
    if ctype == "dish":
        r = conn.execute("SELECT identity_card, description FROM dishes WHERE name = ?",
                         (ckey,)).fetchone()
        if r:
            try:
                identity = json.loads(r["identity_card"]) if r["identity_card"] else None
            except Exception:
                identity = None
            if not identity and r["description"]:
                identity = {"description": r["description"]}

    kept = []
    for k in mt["kept"]:
        kept.append({"url_normalized": k["url_normalized"], "url": k["url"],
                     "title": k["title"], "rank": k["final_rank"],
                     "da": k["da"], "pa": k["pa"], "ou": k["ou"],
                     "recipe": _kept_evidence(conn, k["url_normalized"])})
    reconsider = [{"url_normalized": r["url_normalized"], "url": r["url"],
                   "title": r["title"], "stage": r["stage"], "reason": r["reason"],
                   "da": r["da"], "pa": r["pa"], "ou": r["ou"]}
                  for r in mt["reconsider"]]
    return {"collection_type": ctype, "collection_key": ckey, "identity": identity,
            "kept": kept, "reconsider": reconsider,
            "excluded_as_fact": mt["excluded_as_fact"]}


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
_SYSTEM = """You are the editor of a curated recipe index. A statistical pass has \
already run: it selected candidates, scored each page's authority (Moz DA/PA) and \
ranked them by OU — how much a page over-performs what its domain's authority \
predicts. Your job is to say where that arithmetic got the EDITORIAL question wrong.

The arithmetic measures link-earning. It does not measure whether a recipe is the \
best version of the dish. It has been observed ranking a six-ingredient \
instant-noodle toss above a from-scratch tonkotsu broth, because the toss sat on a \
mid-authority domain and the broth did not.

JUDGE ON FIVE AXES:
1. DISH FIDELITY  - is this actually the dish, as the identity card defines it? A \
   shortcut using the dish's name as a flavouring is not the dish.
2. METHOD COMPLETENESS - does it do the dish's defining work, or shortcut it?
3. CRAFT SPECIFICITY - real quantities, temperatures, times, doneness cues.
4. SOURCE TRUST - the DA/PA/OU you are given, read as evidence and not as a verdict.
5. FAILURE-MODE COVERAGE - does it address the way this dish characteristically \
   goes wrong? The dishes worth ranking are the ones with a contested technique.

RULES YOU MUST FOLLOW:
- CITE. Every judgement names a fact from the record: an ingredient, a step, a \
  count, a number. "Feels inauthentic" is not a reason. "Uses a stock cube where \
  the dish is defined by a 6-hour pork bone broth" is.
- RANK BEFORE YOU RATE. Order the kept set best-to-worst first. Bands must then be \
  monotonic with that order — you may not band a recipe above one you ranked higher.
- BANDS ARE 3.0 to 5.0 in half steps. Nothing scores below 3.0: everything here \
  already survived selection, and the index IS the filter. Reserve 5.0 for a \
  reference version of the dish. Do not cluster: if everything is 4.0 you have not \
  judged anything.
- YOU CANNOT SEE THE DROPPED PAGES. For anything in `reconsider` you have a title, \
  a URL and three numbers — nothing else. So you may NOT promote one. You may \
  `nominate` it, which means: this is worth paying to fetch and judge properly. \
  Nominate on the evidence of the title and the source, and say what you expect.
- PREFER `hold`. Disagreeing with the arithmetic is the exception; if the ranking \
  looks right, say so. A pass that demotes half the set is a broken pass.
- If you cannot tell, `flag` it and say what you would need."""

_EMIT_TOOL = {
    "name": "emit_mediation",
    "description": "Record the editorial review of one run.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ranking": {
                "type": "array",
                "description": "url_normalized of every KEPT recipe, best first. "
                               "Must contain each kept url exactly once.",
                "items": {"type": "string"},
            },
            "verdicts": {
                "type": "array",
                "description": "One entry per KEPT recipe.",
                "items": {
                    "type": "object",
                    "properties": {
                        "url_normalized": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["hold", "demote", "flag"]},
                        "band": {"type": "number",
                                 "description": "3.0-5.0 in half steps, monotonic with ranking."},
                        "axes": {
                            "type": "object",
                            "properties": {
                                "dish_fidelity": {"type": "string"},
                                "method_completeness": {"type": "string"},
                                "craft_specificity": {"type": "string"},
                                "source_trust": {"type": "string"},
                                "failure_mode_coverage": {"type": "string"},
                            },
                            "required": ["dish_fidelity", "method_completeness"],
                        },
                        "evidence": {"type": "string",
                                     "description": "Facts cited FROM the record. No adjectives alone."},
                        "rationale": {"type": "string"},
                    },
                    "required": ["url_normalized", "verdict", "band", "evidence"],
                },
            },
            "nominations": {
                "type": "array",
                "description": "Dropped URLs worth paying to fetch and judge. May be empty.",
                "items": {
                    "type": "object",
                    "properties": {
                        "url_normalized": {"type": "string"},
                        "expectation": {"type": "string",
                                        "description": "What you expect to find, and why."},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["url_normalized", "expectation", "confidence"],
                },
            },
            "summary": {"type": "string",
                        "description": "Two sentences: did the arithmetic get this run right?"},
        },
        "required": ["ranking", "verdicts", "summary"],
    },
}


def _user_message(packet: dict) -> str:
    ident = packet.get("identity")
    parts = [f"COLLECTION: {packet['collection_type']} = {packet['collection_key']}"]
    if ident:
        parts.append("IDENTITY CARD (what this dish IS — judge fidelity against this):\n"
                     + json.dumps(ident, ensure_ascii=False, indent=1)[:2500])
    else:
        parts.append("IDENTITY CARD: none on file — judge fidelity from the dish name.")
    parts.append("\nKEPT (selected by the statistical pass; you hold their content):\n"
                 + json.dumps(packet["kept"], ensure_ascii=False, indent=1))
    parts.append("\nRECONSIDER (dropped by a JUDGEMENT, not a fact — title/URL/scores "
                 "only, so nominate at most):\n"
                 + json.dumps(packet["reconsider"], ensure_ascii=False, indent=1))
    parts.append(f"\n({packet['excluded_as_fact']} further drops were matters of fact — "
                 f"blocklist, archive pages, roundups — and are deliberately not shown.)")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def mediate_run(conn: sqlite3.Connection, job_id: int, *, apply: bool = False,
                dry_run: bool = False) -> dict:
    """Review one run. SHADOW by default: writes verdicts, changes nothing.

    `apply=True` is Phase 2 and is deliberately not implemented — it raises rather
    than silently doing nothing, so nobody believes it took effect.
    `dry_run=True` builds the packet and calls no model (for inspecting cost/shape).
    """
    if apply:
        raise NotImplementedError(
            "apply=True is Phase 2 (docs/ai-editor-mediation.md). Shadow mode records "
            "verdicts for calibration; giving them effect needs the override plumbing "
            "and a measured agreement rate first.")
    ensure_mediation_table(conn)
    packet = build_packet(conn, job_id)
    if not packet:
        return {"job_id": job_id, "error": "no ledger for this run"}
    if dry_run:
        msg = _user_message(packet)
        return {"job_id": job_id, "kept": len(packet["kept"]),
                "reconsider": len(packet["reconsider"]),
                "approx_prompt_chars": len(msg) + len(_SYSTEM)}

    resp = llm.create(
        operation="ai_editor", model=MODEL, max_tokens=_MAX_TOKENS, system=_SYSTEM,
        tools=[_EMIT_TOOL], tool_choice={"type": "tool", "name": "emit_mediation"},
        messages=[{"role": "user", "content": _user_message(packet)}],
    )
    if resp.stop_reason == "max_tokens":
        # Same trap cook_rework documents: a forced tool_use that hits the cap
        # returns PARTIAL json, and the missing tail reads as "no verdicts".
        raise RuntimeError(
            f"emit_mediation truncated at max_tokens ({_MAX_TOKENS}) — output "
            f"incomplete, NOT persisting")
    out = None
    for block in resp.content:
        if block.type == "tool_use":
            out = block.input
            break
    if out is None:
        raise RuntimeError("emit_mediation tool was not called (tool_choice should have forced it)")

    usages: list = []
    cook_costs.record(usages, MODEL, resp.usage)
    try:
        cost = float(cook_costs.estimate(usages).get("total") or 0.0)
    except Exception:
        cost = None
    print(f"[ai-editor] {MODEL}: {resp.usage.input_tokens} in / "
          f"{resp.usage.output_tokens} out"
          + (f" ≈ ${cost:.3f}" if cost is not None else ""))
    return _persist(conn, packet, out, job_id, cost)


def _persist(conn: sqlite3.Connection, packet: dict, out: dict, job_id: int,
             cost: Optional[float]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    order = {u: i + 1 for i, u in enumerate(out.get("ranking") or [])}
    by_url = {k["url_normalized"]: k for k in packet["kept"]}
    recon = {r["url_normalized"]: r for r in packet["reconsider"]}
    rows = []

    for v in out.get("verdicts") or []:
        u = v.get("url_normalized")
        src = by_url.get(u)
        if not src:      # the model named a url that was not in the kept set
            continue
        rows.append((job_id, packet["collection_type"], packet["collection_key"],
                     src["url"], u, v.get("verdict") or "hold", order.get(u),
                     v.get("band"), json.dumps(v.get("axes") or {}, ensure_ascii=False),
                     v.get("evidence"), v.get("rationale"), "ai", MODEL,
                     PROMPT_VERSION, cost, 0, None, None, None, now))
    for n in out.get("nominations") or []:
        u = n.get("url_normalized")
        src = recon.get(u)
        if not src:      # only ever nominate from what it was allowed to see
            continue
        rows.append((job_id, packet["collection_type"], packet["collection_key"],
                     src["url"], u, "nominate", None, None,
                     json.dumps({"confidence": n.get("confidence")}, ensure_ascii=False),
                     n.get("expectation"), n.get("expectation"), "ai", MODEL,
                     PROMPT_VERSION, cost, 0, None, None, None, now))

    conn.executemany(
        "INSERT INTO run_mediations (job_id, collection_type, collection_key, url, "
        "url_normalized, verdict, ordinal_rank, band, axes, evidence, rationale, actor, "
        "model, prompt_version, cost_usd, applied, human_verdict, human_note, human_at, "
        "created_at) VALUES (" + ",".join("?" * 20) + ") "
        "ON CONFLICT(job_id, url_normalized, actor) DO UPDATE SET "
        "verdict=excluded.verdict, ordinal_rank=excluded.ordinal_rank, band=excluded.band, "
        "axes=excluded.axes, evidence=excluded.evidence, rationale=excluded.rationale, "
        "model=excluded.model, prompt_version=excluded.prompt_version, "
        "cost_usd=excluded.cost_usd, created_at=excluded.created_at",
        rows)
    conn.commit()

    counts: dict[str, int] = {}
    for r in rows:
        counts[r[5]] = counts.get(r[5], 0) + 1
    # Did the editor reorder the winners? That single number is the headline of a
    # shadow run — it is the disagreement the whole exercise exists to measure.
    moved = sum(1 for k in packet["kept"]
                if order.get(k["url_normalized"]) not in (None, k["rank"]))
    return {"job_id": job_id, "collection_key": packet["collection_key"],
            "written": len(rows), "verdicts": counts, "rank_changes": moved,
            "summary": out.get("summary"), "cost_usd": cost, "applied": False}


def list_for_run(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM run_mediations WHERE job_id = ? "
        "ORDER BY ordinal_rank IS NULL, ordinal_rank", (job_id,))]
