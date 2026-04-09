"""Integration gate: assemble passing branches and run the full test suite."""

from datetime import datetime, timezone

from . import SPECS_DIR
from .git_ops import branch_exists, checkout, merge_branch, delete_branch
from .log import get_logger
from .runner import run_full_suite
from .state import save_state

log = get_logger("integration")


def integration_gate(passed_task_names, default_branch, state):
    """
    Assemble an integration branch by merging all passing task branches and
    run the full pytest suite against the result.

    Behaviour:
    - Creates ``integration/run-<timestamp>`` from the default branch.
    - Merges each passing task branch (in the order provided) with --no-ff.
    - On merge conflict or full-suite failure, writes
      ``specs/INTEGRATION-FAILED.log``, deletes the integration branch,
      and returns False so the caller can block merge.
    - On success, leaves the integration branch in place for human review
      and returns True.

    The caller is responsible for returning to the default branch afterwards.
    """
    if not passed_task_names:
        log.info("\n[integration gate] No passing tasks -- skipping.")
        return True

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

    log_path = SPECS_DIR / "INTEGRATION-FAILED.log"

    if failed_merges:
        msg = (
            f"Integration gate failed: merge conflict on {failed_merges[0]}.\n"
            "The per-task branches individually pass their own tests, but "
            "they cannot be combined cleanly. Resolve conflicts manually or "
            "rework the spec dependencies.\n"
        )
        log_path.write_text(msg)
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
        log_path.write_text(
            f"Integration gate failed: full test suite failed on {integration_branch}.\n\n"
            f"{output}\n"
        )
        log.error("  INTEGRATION FAILED (test suite). See %s", log_path)
        state["integration"] = {
            "branch": integration_branch,
            "status": "tests_failed",
            "timestamp": timestamp,
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
