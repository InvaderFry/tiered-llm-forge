"""Integration gate: assemble passing branches and run the full test suite."""

import re as _re
from datetime import datetime, timezone

from . import FORGE_LOGS_DIR
from .config import get_tier
from .git_ops import branch_exists, checkout, merge_branch, delete_branch
from .log import get_logger, reserve_log_path, write_timestamped_log
from .model_router import run_with_tier_fallback, all_gemini_quota_exhausted
from .runner import run_full_suite
from .state import save_state

log = get_logger("integration")

# ---------------------------------------------------------------------------
# Gemini fix helpers
# ---------------------------------------------------------------------------

_TRACEBACK_SRC_RE = _re.compile(r"(src/[^\s:]+\.py)")


def _extract_failing_src_files(test_output: str, fallback=None) -> list:
    """Extract up to 5 unique src/ paths from a pytest traceback.

    pytest --tb=short prints lines like ``src/models/user.py:42: in fn``.
    We collect paths in first-seen order (correlates with the deepest call
    site). Falls back to ``fallback`` list when nothing is found, then to
    ``src/__init__.py`` as a stub so aider always gets a valid --file arg.
    """
    seen, seen_set = [], set()
    for m in _TRACEBACK_SRC_RE.finditer(test_output):
        p = m.group(1)
        if p not in seen_set:
            seen_set.add(p)
            seen.append(p)
            if len(seen) >= 5:
                break
    if not seen and fallback:
        seen = list(fallback)[:1]
    return seen or ["src/__init__.py"]


def _attempt_gemini_integration_fix(test_output: str, passed_task_names: list) -> bool:
    """Attempt to fix integration test failures using the Gemini tier.

    Runs on the currently checked-out branch (the integration branch).
    Returns True if the full test suite passes after Gemini's fix attempt,
    False otherwise (including when daily quota is exhausted for all models).
    """
    try:
        get_tier("gemini")
    except ValueError:
        log.info("  [integration Gemini fix: gemini tier not configured -- skipping]")
        return False

    if all_gemini_quota_exhausted():
        log.warning("  [integration Gemini fix: all models daily-quota-exhausted]")
        return False

    target_files = _extract_failing_src_files(test_output)
    primary_target = target_files[0]
    read_files = target_files[1:]

    message = (
        f"Integration test suite failed. pytest output:\n{test_output}\n\n"
        f"Fix the source files to make all tests pass. "
        f"Focus on {primary_target}. Do not modify test files."
    )
    log.info("  [integration] trying Gemini fix (target: %s)...", primary_target)
    success, model_used, _, _ = run_with_tier_fallback(
        "gemini", message, primary_target, read_files=read_files,
    )
    if not success:
        if all_gemini_quota_exhausted():
            log.warning("  [integration Gemini fix: all models daily-quota-exhausted after attempt]")
        else:
            log.warning("  [integration Gemini fix: model returned failure]")
        return False

    log.info("  [integration] Gemini fix committed by %s -- re-running full suite...", model_used)
    passed, _ = run_full_suite("tests")
    if passed:
        log.info("  INTEGRATION PASSED after Gemini fix by %s.", model_used)
    else:
        log.warning("  [integration Gemini fix: suite still failing after fix by %s]", model_used)
    return passed


def integration_gate(passed_task_names, default_branch, state):
    """
    Assemble an integration branch by merging all passing task branches and
    run the full pytest suite against the result.

    Behaviour:
    - Creates ``integration/run-<timestamp>`` from the default branch.
    - Merges each passing task branch (in the order provided) with --no-ff.
    - On merge conflict or full-suite failure, writes
      ``forgeLogs/INTEGRATION-FAILED-<timestamp>.log``, deletes the integration branch,
      and returns False so the caller can block merge.
    - On success, leaves the integration branch in place for human review
      and returns True.

    The caller is responsible for returning to the default branch afterwards.
    """
    if not passed_task_names:
        log.info("\n[integration gate] No passing tasks -- skipping.")
        return True

    gate_started_at = datetime.now().astimezone()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    integration_branch = f"integration/run-{timestamp}"

    log.info("\n%s", "=" * 50)
    log.info("INTEGRATION GATE: %s", integration_branch)
    log.info("=" * 50)

    # Record where we came from so we can always get back to it
    checkout(default_branch)
    checkout(integration_branch, create=True, start_point=default_branch)

    failed_merges = []
    for task_name in passed_task_names:
        branch = f"task/{task_name}"
        if not branch_exists(branch):
            log.info("  [skip] %s -- branch missing", branch)
            continue
        log.info("  merging %s", branch)
        if not merge_branch(branch, message=f"integration: merge {branch}"):
            failed_merges.append(task_name)
            break

    if failed_merges:
        FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = reserve_log_path("INTEGRATION-FAILED")
        msg = (
            f"Integration gate failed: merge conflict on {failed_merges[0]}.\n"
            "The per-task branches individually pass their own tests, but "
            "they cannot be combined cleanly. Resolve conflicts manually or "
            "rework the spec dependencies.\n"
        )
        write_timestamped_log(log_path, msg, started_at=gate_started_at)
        log.error("\n  INTEGRATION FAILED (merge conflict). See %s", log_path)
        state["integration"] = {
            "branch": integration_branch,
            "status": "merge_conflict",
            "failed_on": failed_merges[0],
            "timestamp": timestamp,
        }
        save_state(state)
        checkout(default_branch)
        delete_branch(integration_branch, force=True)
        return False

    # Merges clean -- run the full suite
    log.info("  running full test suite on integration branch...")
    passed, output = run_full_suite("tests")
    if not passed:
        log.info("  full suite failed -- attempting Gemini fix...")
        if _attempt_gemini_integration_fix(output, passed_task_names):
            passed = True  # fall through to the success path below
        else:
            gemini_exhausted = all_gemini_quota_exhausted()
            FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = reserve_log_path("INTEGRATION-FAILED")
            quota_note = (
                "\nGemini tier also attempted but daily quota exhausted.\n"
                if gemini_exhausted else ""
            )
            write_timestamped_log(
                log_path,
                (
                    f"Integration gate failed: full test suite failed on {integration_branch}.\n"
                    f"{quota_note}\n{output}\n"
                ),
                started_at=gate_started_at,
            )
            log.error("  INTEGRATION FAILED (test suite). See %s", log_path)
            state["integration"] = {
                "branch": integration_branch,
                "status": "tests_failed",
                "timestamp": timestamp,
                "gemini_quota_exhausted": gemini_exhausted,
            }
            save_state(state)
            checkout(default_branch)
            # Keep the branch around so the human can reproduce the failure
            return False

    log.info("  INTEGRATION PASSED: %s is ready to merge.", integration_branch)
    state["integration"] = {
        "branch": integration_branch,
        "status": "passed",
        "timestamp": timestamp,
    }
    save_state(state)
    checkout(default_branch)
    return True
