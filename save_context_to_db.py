import sqlite3
import json
import uuid
import sys
from datetime import datetime, timezone

def save_context_entries_to_db(context_path: str):
    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)

    conn = sqlite3.connect("recipes.db", timeout=10)
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    inserted = 0
    for key, entry in context.items():
        if entry.get("current_status") != "accepted":
            continue

        # Generate or reuse recipe_id
        recipe_id = entry.get("id") or str(uuid.uuid4())
        entry["id"] = recipe_id  # persist back into context

        # Clean out internal keys
        data_to_store = {k: v for k, v in entry.items() if k not in ["history", "current_status"]}

        try:
            cur.execute("""
                INSERT INTO recipes (recipe_id, user_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (recipe_id, 1, json.dumps(data_to_store), now, now))
            inserted += 1

            # Log the successful insert
            name = entry.get("title", {}).get("value") or entry.get("name") or "Unnamed"
            print(f"     ➕ Inserted: {key}  |  {name}")

        except sqlite3.IntegrityError as e:
            print(f"     ❌ Skipping duplicate or invalid record (key: {key}): {e}")

    conn.commit()
    conn.close()
    print(f"     ✅  Saved {inserted} recipe(s) to database.")

if __name__ == "__main__":
    # Program start message with timestamp
    start_time = datetime.now(timezone.utc)
    print(f"\n>>>> Start: save_context_to_db.py at {start_time.isoformat()}")

    if len(sys.argv) != 2:
        print("Usage: python save_context_to_db.py path/to/context.json")
        sys.exit(1)

    save_context_entries_to_db(sys.argv[1])

    # Program end message with timestamp
    end_time = datetime.now(timezone.utc)
    print(f">>>> End: save_context_to_db.py at {end_time.isoformat()}\n")

