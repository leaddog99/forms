"""Backfill: run enrich_recipe on every master_recipes row whose LLM
enrichment is missing.

The "pay-once enrichment" property promises that anyone who claims a
master recipe inherits its provenance/classification/editorial via
recipe_model.static_subset. Most existing master rows pre-date Enrich
being run on them — they have empty `classification.story`, empty
`provenance.ethnicity`, no `editorial`. This script fills those rows
in one shot using the same `enrich_recipe` call the form's Enrich
button uses.

Usage (from the project root):
  python -m scripts.backfill_master_enrichment              # default --limit 5
  python -m scripts.backfill_master_enrichment --limit 20
  python -m scripts.backfill_master_enrichment --dry-run    # report only
  python -m scripts.backfill_master_enrichment --limit 0    # no cap

Idempotent: re-runs skip rows that already have a populated
`classification.story`. Writes token-journal rows so cost shows up in
bcc_token_journal alongside extract-time usage.

Notes:
- Uses claude-haiku-4-5 (the model enrich_recipe defaults to as of the
  2026-05-22 Anthropic migration). Now fans out to three parallel
  blocks (provenance / classification / editorial), so wall time is
  ~7-11s per row depending on which block is slowest.
- This is a manual maintenance script, not an endpoint. Cost is
  intentional and bounded by --limit.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# load_dotenv BEFORE importing enrich_recipe — the module constructs
# its anthropic.Anthropic() client at import and the SDK reads
# ANTHROPIC_API_KEY at construction time (caches None permanently if
# env is empty at that moment).
load_dotenv()

# Ensure the project root is importable when run via `python -m`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from extract.enrich_recipe import enrich_recipe  # noqa: E402
from input.pipeline.token_journal import write_usage_entries  # noqa: E402

DB_PATH = str(PROJECT_ROOT / "recipes.db")
MASTER_USER_ID = 0  # journal tag for master rows


def needs_enrichment(data: dict) -> bool:
    """A row needs enrichment when the LLM's biggest unique output —
    classification.story — is empty. story is 150-300 words when the
    LLM ran successfully; if it's blank, neither the enrich call has
    been made for this row nor has it been hand-curated with a story.
    """
    cls = data.get("classification") or {}
    story = (cls.get("story") or "").strip()
    return not story


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=5,
                        help="Max rows to enrich this run. 0 disables the "
                             "cap (process everything). Default 5.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Identify candidates and report counts; do "
                             "not call the LLM or write to the DB.")
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        # Pull all master rows. Filter in Python — json_extract path
        # behavior across NULL/missing is finicky and the table is small.
        all_rows = conn.execute(
            "SELECT recipe_id, data FROM master_recipes ORDER BY id"
        ).fetchall()

        candidates = []
        for recipe_id, data_json in all_rows:
            try:
                data = json.loads(data_json) if data_json else {}
            except json.JSONDecodeError:
                print(f"  WARN: {recipe_id} has invalid JSON; skipping")
                continue
            if needs_enrichment(data):
                candidates.append((recipe_id, data))

        cap = args.limit if args.limit > 0 else len(candidates)
        batch = candidates[:cap]

        print(f"master_recipes total       : {len(all_rows)}")
        print(f"missing enrichment         : {len(candidates)}")
        print(f"processing this run        : {len(batch)} (limit={args.limit})")
        print()

        if args.dry_run:
            for rid, data in batch:
                print(f"  [dry-run] {rid}  {data.get('name')!r}")
            return 0

        processed = 0
        total_input_tokens = 0
        total_output_tokens = 0
        for idx, (rid, data) in enumerate(batch, start=1):
            print(f"[{idx}/{len(batch)}] {rid}  {data.get('name')!r}")
            usage_log: list = []
            timings: dict = {}
            try:
                enrich_recipe(data, timings=timings, usage_log=usage_log)
            except Exception as e:
                print(f"   ERROR during enrich_recipe: {e}")
                continue

            new_story = (data.get("classification") or {}).get("story") or ""
            if not new_story.strip():
                # enrich_recipe never raises; on LLM/parse failure it
                # just leaves defaults. If the story is still empty
                # after the call, the LLM didn't produce one — don't
                # write a no-op row back.
                print(f"   WARN: enrichment produced no story; skipping DB write")
                continue

            now = datetime.utcnow().isoformat()
            conn.execute(
                "UPDATE master_recipes SET data = ?, updated_at = ? "
                "WHERE recipe_id = ?",
                (json.dumps(data, indent=2), now, rid),
            )
            write_usage_entries(
                conn,
                user_id=MASTER_USER_ID,
                recipe_id=rid,
                entries=usage_log,
            )
            conn.commit()

            in_tok = usage_log[0]["input_tokens"] if usage_log else 0
            out_tok = usage_log[0]["output_tokens"] if usage_log else 0
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            print(f"   OK  story={len(new_story)} chars  "
                  f"tokens={in_tok}+{out_tok}  "
                  f"llm_ms={timings.get('enrich_llm_ms')}")
            processed += 1

        print()
        print(f"Done. Enriched {processed}/{len(batch)} row(s).")
        print(f"Total tokens this run: {total_input_tokens} in + "
              f"{total_output_tokens} out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
