import os
import json
import sys
from datetime import datetime, timezone


def ingest_image(image_path, context_path):
    now = datetime.now(timezone.utc).isoformat()
    # Ensure parent directory exists
    os.makedirs(os.path.dirname(context_path), exist_ok=True)
    # Load or initialize context.json
    if os.path.exists(context_path):
        try:
            with open(context_path, "r") as f:
                context = json.load(f)
        except (json.JSONDecodeError, ValueError):
            print(f"     ⚠️  Invalid JSON in {context_path}. Initializing empty context.")
            context = {}
    else:
        context = {}
    # Add image entry
    context[image_path] = {
        "source_type": "image",
        "input_image": image_path,
        "history": [
            {
                "step": "ingest_image",
                "timestamp": now,
                "status": "complete"
            }
        ],
        "current_status": "accepted"
    }
    # Save updated context
    with open(context_path, "w") as f:
        json.dump(context, f, indent=2)
    print(f"     ✅  Image '{image_path}' ingested into context: {context_path}")


if __name__ == "__main__":
    # Program start message with timestamp
    start_time = datetime.now(timezone.utc)
    print(f"\n>>>> Start: ingest_image.py at {start_time.isoformat()}")

    if len(sys.argv) != 3:
        print("Usage: python ingest_image.py <image_path> <context_path>")
        sys.exit(1)

    image_path = sys.argv[1]
    context_path = sys.argv[2]
    ingest_image(image_path, context_path)

    # Program end message with timestamp
    end_time = datetime.now(timezone.utc)
    print(f">>>> End: ingest_image.py at {end_time.isoformat()}\n")