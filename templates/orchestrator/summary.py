"""Pipeline run summary and observability roll-up."""

from . import FORGE_LOGS_DIR
from .log import get_logger

log = get_logger("summary")


def print_summary(results, default_branch, state=None):
    """Print final summary of pipeline run."""
    passed = results["passed"]
    failed = results["failed"]
    skipped = results["skipped"]
    blocked = results.get("blocked", [])

    log.info("\n%s", "=" * 50)
    log.info("PASSED:  %d", len(passed))
    log.info("SKIPPED: %d (already passing)", len(skipped))
    log.info("BLOCKED: %d (dependency failure)", len(blocked))
    log.info("FAILED:  %d", len(failed))

    # Observability roll-up: per-model success/failure counts, task
    # timings, and token / cost totals scraped from aider's stdout.
    if state and state.get("tasks"):
        attempted_by_model = {}
        total_time = 0.0
        total_tokens_sent = 0
        total_tokens_received = 0
        total_cost = 0.0
        for entry in state["tasks"].values():
            dur = entry.get("duration_seconds") or 0.0
            total_time += dur
            total_tokens_sent += entry.get("tokens_sent", 0) or 0
            total_tokens_received += entry.get("tokens_received", 0) or 0
            total_cost += entry.get("cost_usd", 0.0) or 0.0
            for attempt in entry.get("attempts_log", []):
                model_attempts = attempt.get("model_attempts")
                if model_attempts:
                    for model_attempt in model_attempts:
                        model = model_attempt.get("model") or "(none)"
                        bucket = attempted_by_model.setdefault(
                            model,
                            {"attempts": 0, "aider_successes": 0, "test_passes": 0},
                        )
                        bucket["attempts"] += 1
                        if model_attempt.get("success"):
                            bucket["aider_successes"] += 1
                            if attempt.get("tests_passed"):
                                bucket["test_passes"] += 1
                    continue

                model = attempt.get("model") or "(none)"
                bucket = attempted_by_model.setdefault(
                    model,
                    {"attempts": 0, "aider_successes": 0, "test_passes": 0},
                )
                bucket["attempts"] += 1
                if attempt.get("aider_success"):
                    bucket["aider_successes"] += 1
                    if attempt.get("tests_passed"):
                        bucket["test_passes"] += 1

        log.info("\nTotal task time: %.1fs", total_time)
        if total_tokens_sent or total_tokens_received or total_cost:
            log.info(
                "Total tokens: %s sent / %s received    Total cost: $%.4f",
                f"{total_tokens_sent:,}", f"{total_tokens_received:,}", total_cost,
            )
        if attempted_by_model:
            log.info("Attempted models:")
            for model, counts in sorted(attempted_by_model.items()):
                log.info(
                    "  %s: %d attempt(s), %d aider success(es), %d test-passing attempt(s)",
                    model,
                    counts["attempts"],
                    counts["aider_successes"],
                    counts["test_passes"],
                )

        fail_classes = {}
        for entry in state["tasks"].values():
            fc = entry.get("failure_class")
            if fc:
                fail_classes[fc] = fail_classes.get(fc, 0) + 1
        if fail_classes:
            log.info("Failure classes:")
            for fc, n in sorted(fail_classes.items(), key=lambda x: -x[1]):
                log.info("  %s: %d", fc, n)

    if blocked:
        log.info("\nBlocked tasks:")
        for task_name in blocked:
            entry = state.get("tasks", {}).get(task_name, {}) if state else {}
            deps = entry.get("blocked_by") or []
            detail = ", ".join(deps) if deps else "failed dependencies"
            log.info("  %s (%s)", task_name, detail)

    if failed:
        log.info("\n%s", "=" * 50)
        log.info("Claude review needed for these failures:")

        # Warn if Gemini daily quota was exhausted for any failed tasks so the
        # user knows that Gemini was already tried and isn't just unconfigured.
        if state and state.get("tasks"):
            gemini_exhausted_count = sum(
                1 for name in failed
                if "gemini_quota_exhausted" in (
                    state["tasks"].get(name, {}).get("llm_fail_reasons") or []
                )
            )
            if gemini_exhausted_count:
                log.info(
                    "  Note: Gemini daily quota was exhausted for %d task(s). "
                    "Retry tomorrow once quota resets, or fix with Claude now.",
                    gemini_exhausted_count,
                )

        for f in failed:
            logs = sorted(FORGE_LOGS_DIR.glob(f"FAILED-{f}-*.log")) if FORGE_LOGS_DIR.exists() else []
            fail_log = logs[-1] if logs else FORGE_LOGS_DIR / f"FAILED-{f}-[timestamp].log"
            log.info("\n  Task:   %s", f)
            log.info("  Branch: task/%s", f)
            log.info("  Log:    %s", fail_log)
            log.info("  Prompt: > Fix %s. The log is at %s.", f, fail_log)
        log.info("\nAfter Claude fixes each failure, re-run: python3 -m orchestrator")
        log.info("Fixed tasks are detected as passing and skipped automatically.")
    elif blocked:
        log.info("\n%s", "=" * 50)
        log.info("Some tasks were blocked by earlier dependency failures.")
        log.info("Fix the failed upstream tasks, then re-run the orchestrator.")
    else:
        log.info("\n%s", "=" * 50)
        log.info("All tasks passing -- ready to merge.")
        log.info("Open Claude Code and say: 'Review the task branches and merge them.'\n")
