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


def record_task(
    state,
    task_name,
    status,
    model=None,
    attempts=0,
    duration_seconds=None,
    models_tried=None,
    failure_class=None,
    base_branch=None,
    base_sha=None,
    tokens_sent=None,
    tokens_received=None,
    cost_usd=None,
):
    """Record the outcome of a single task.

    Extra fields (all optional) provide the observability hooks needed to
    answer questions like "which model fails most often?", "how long
    does each task take?", and "is the escalation tier worth its cost?"
    without scraping log files.
    """
    entry = {
        "status": status,
        "model": model,
        "attempts": attempts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if duration_seconds is not None:
        entry["duration_seconds"] = round(float(duration_seconds), 2)
    if models_tried is not None:
        entry["models_tried"] = list(models_tried)
    if failure_class is not None:
        entry["failure_class"] = failure_class
    if base_branch is not None:
        entry["base_branch"] = base_branch
    if base_sha is not None:
        entry["base_sha"] = base_sha
    if tokens_sent is not None:
        entry["tokens_sent"] = int(tokens_sent)
    if tokens_received is not None:
        entry["tokens_received"] = int(tokens_received)
    if cost_usd is not None:
        entry["cost_usd"] = round(float(cost_usd), 6)

    state["tasks"][task_name] = entry
    return state
