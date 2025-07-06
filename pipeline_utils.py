import json
from pathlib import Path

def load_context(context_path):
    if Path(context_path).exists():
        return json.loads(Path(context_path).read_text())
    return {}

def save_context(context_path, context):
    Path(context_path).parent.mkdir(parents=True, exist_ok=True)
    Path(context_path).write_text(json.dumps(context, indent=2))

def append_history_entry(url_data, step_name, status, module, reason=None):
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "step": step_name,
        "status": status,
        "module": module,
    }
    if reason:
        entry["reason"] = reason
    url_data.setdefault("history", []).append(entry)
