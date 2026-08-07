"""Generate forms/stars_demo.json — real corpus recipes carrying ONLY the public
payload, so the mockup proves the endpoint contract as well as the look.

Deliberately emits no ou/power/blend/percentile. If you can reconstruct a score
from this file, the chokepoint has a hole in it.
"""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from input.pipeline.public_scoring import public_score, BADGE_TEXT  # noqa: E402

DB = ROOT / "recipes.db"
OUT = ROOT / "forms" / "stars_demo.json"

con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT id, source_host,
           json_extract(data,'$.name')                 AS title,
           json_extract(data,'$._source.previewImage') AS prev,
           coalesce(json_extract(data,'$.image[0]'),
                    json_extract(data,'$.image'))      AS img0,
           PERCENT_RANK() OVER (ORDER BY ou_score) AS ou_pct,
           PERCENT_RANK() OVER (ORDER BY power)    AS pw_pct
    FROM master_recipes
    WHERE ou_score IS NOT NULL AND power IS NOT NULL
""").fetchall()
print(f"scored rows: {len(rows)}")

items = []
for r in rows:
    pub = public_score(r["ou_pct"], r["pw_pct"])
    if not pub or not r["title"]:
        continue
    items.append({
        "title": r["title"],
        "host": r["source_host"],
        "thumb": r["prev"] or r["img0"] or "",
        **pub,
        "badgeText": BADGE_TEXT.get(pub["badge"]) if pub["badge"] else None,
    })

dist = Counter(i["stars"] for i in items)
print("distribution:", sorted(dist.items()))
print("badges:", Counter(i["badge"] for i in items if i["badge"]))

# A spread for the mockup: some of every level, both badges represented,
# preferring rows that actually have a thumbnail so the row looks real.
picked, seen = [], Counter()
for want in (5.0, 4.5, 4.0, 3.5, 3.0):
    pool = [i for i in items if i["stars"] == want and i["thumb"]]
    pool.sort(key=lambda i: (i["badge"] is None, i["title"]))
    for i in pool:
        if seen[want] >= 6:
            break
        picked.append(i)
        seen[want] += 1

payload = {
    "_note": "Demo data for forms/stars_mockup.html. PUBLIC FIELDS ONLY — this "
             "is the exact shape a user-facing endpoint may return. No ou, "
             "power, blend or percentile appears here by design.",
    "counts": {str(k): v for k, v in sorted(dist.items())},
    "total": len(items),
    "items": picked,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)
print(f"wrote {OUT}  ({len(picked)} rows)")
