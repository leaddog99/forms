import sqlite3
import json
import tempfile
import os

conn = sqlite3.connect('recipes.db')
cur = conn.cursor()
cur.execute("SELECT id, data FROM recipes WHERE id = ?", (1,))
row = cur.fetchone()
if not row:
    print("No record found.")
    exit()
recipe_id, data = row

# Write JSON to a temp file
with tempfile.NamedTemporaryFile('w+', delete=False, suffix='.json') as tf:
    tf.write(data or '{}')
    tf.flush()
    # Launch PyCharm with the temp file
    pycharm_path = r"C:\Program Files\JetBrains\PyCharm 2024.1\bin\pycharm64.exe"  # Update as needed
    os.system(f'"{pycharm_path}" "{tf.name}"')
    # After you edit and save in PyCharm, press Enter to continue
    input("After editing and saving in PyCharm, press Enter here to continue...")
    # Reopen to read updated content
    with open(tf.name, 'r') as f:
        updated_data = f.read()

# Validate and update
try:
    json.loads(updated_data)
    cur.execute("UPDATE recipes SET data = ? WHERE id = ?", (updated_data, recipe_id))
    conn.commit()
    print("Recipe updated.")
except Exception as e:
    print("Invalid JSON, not saved:", e)

conn.close()
