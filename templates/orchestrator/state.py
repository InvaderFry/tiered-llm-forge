"""Pipeline state persistence for crash recovery and reporting."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("pipeline-state.json")

# Protects concurrent access to the state dict and file in --parallel mode
_state_lock = threading.Lock()


def load_state(path=None):
    """Load pipeline state from disk, or return empty state."""
    state_path = path or STATE_FILE
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return _empty_state()


def save_state(state, path=None):
    """Write pipeline state to disk. Thread-safe."""
    state_path = path or STATE_FILE
    with _state_lock:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)


def append_run_summary(state, results):
    """Append a lightweight run-summary entry to ``state["runs"]``.

    ``results`` is the ``{"passed": [...], "failed": [...], "skipped": [...]}``
    dict built by the CLI main loop. Appending here (rather than overwriting
    on load) lets you answer "how many runs has task-003 failed this week?"
    from state alone without scraping log files.
    """
    entry = {
        "run_id": state.get("run_id", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": list(results.get("passed", [])),
        "failed": list(results.get("failed", [])),
        "skipped": list(results.get("skipped", [])),
        "blocked": list(results.get("blocked", [])),
    }
    with _state_lock:
        state.setdefault("runs", []).append(entry)


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
    llm_fail_reasons=None,
    blocked_by=None,
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
    if llm_fail_reasons is not None:
        entry["llm_fail_reasons"] = list(llm_fail_reasons)
    if blocked_by is not None:
        entry["blocked_by"] = list(blocked_by)
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

    with _state_lock:
        state["tasks"][task_name] = entry
    return state


def record_attempt(state, task_name, attempt_num, tier, model, aider_success, tests_passed=None):
    """Record a single attempt within a task for granular crash recovery.

    Stored under ``state["tasks"][task_name]["attempts_log"]`` as a list,
    so ``--resume`` can pick up from the exact point of failure instead of
    re-evaluating the whole branch. Thread-safe.
    """
    with _state_lock:
        entry = state["tasks"].setdefault(task_name, {"status": "in_progress"})
        log = entry.setdefault("attempts_log", [])
        log.append({
            "attempt": attempt_num,
            "tier": tier,
            "model": model,
            "aider_success": aider_success,
            "tests_passed": tests_passed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


def get_resume_point(state, task_name):
    """Determine where to resume a task from its attempt log.

    Returns a dict with:
    - ``tier``: "primary" or "escalation"
    - ``attempt``: the attempt number to start from (1-based)
    - ``total_attempts``: how many attempts have been made so far

    Returns None if no prior attempts exist (fresh start).
    """
    entry = state.get("tasks", {}).get(task_name, {})
    log = entry.get("attempts_log", [])
    if not log:
        return None

    last = log[-1]
    return {
        "tier": last["tier"],
        "attempt": last["attempt"],
        "total_attempts": len(log),
    }
