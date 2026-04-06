"""Pipeline state persistence for crash recovery and reporting."""

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("pipeline-state.json")


def load_state(path=None):
    """Load pipeline state from disk, or return empty state."""
    state_path = path or STATE_FILE
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return _empty_state()


def save_state(state, path=None):
    """Write pipeline state to disk."""
    state_path = path or STATE_FILE
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def _empty_state():
    return {
        "run_id": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tasks": {},
    }


def record_task(state, task_name, status, model=None, attempts=0):
    """Record the outcome of a single task."""
    state["tasks"][task_name] = {
        "status": status,
        "model": model,
        "attempts": attempts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return state
