"""Pipeline run summary and observability roll-up."""

from . import FORGE_LOGS_DIR
from .log import get_logger
from .task_runner import get_run_time_breakdown

log = get_logger("summary")


def _time_bucket_label(reason):
    labels = {
        "productive": "Productive (edit + tests passed)",
        "reverted_or_failed_tests": "Edit produced but reverted/failed tests",
        "forbidden_edit_waste": "Forbidden-edit drift",
        "dependency_forbidden_edit_waste": "Dependency-owned forbidden edits",
    }
    return labels.get(reason, f"Wasted on {reason}")


def print_summary(results, default_branch, state=None):
    """Print final summary of pipeline run."""
    passed = results["passed"]
    failed = results["failed"]
    skipped = results["skipped"]
    blocked = results.get("blocked", [])
    current_task_names = passed + failed + skipped + blocked
    recovered_skips = 0
    already_passing_skips = len(skipped)
    if state and state.get("tasks"):
        recovered_skips = sum(
            1 for name in skipped
            if state["tasks"].get(name, {}).get("verification_status") == "recovered_after_prior_failure"
        )
        already_passing_skips = len(skipped) - recovered_skips

    log.info("\n%s", "=" * 50)
    log.info("PASSED:  %d", len(passed))
    log.info(
        "SKIPPED: %d (%d already passing, %d recovered since last run)",
        len(skipped),
        already_passing_skips,
        recovered_skips,
    )
    log.info("BLOCKED: %d (dependency failure)", len(blocked))
    log.info("FAILED:  %d", len(failed))

    # Observability roll-up: per-model success/failure counts, task
    # timings, and token / cost totals scraped from aider's stdout.
    if state and state.get("tasks"):
        current_entries = [
            state["tasks"][name]
            for name in current_task_names
            if name in state["tasks"]
        ]
        attempted_by_model = {}
        oversized_context = {
            "provider_rejections": 0,
            "fast_skips_after_rejection": 0,
            "pre_screen_skips": 0,
        }
        forbidden_edit_attempts = {"generic": 0, "dependency_target": 0}
        total_time = 0.0
        total_tokens_sent = 0
        total_tokens_received = 0
        total_cost = 0.0
        for entry in current_entries:
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
                        reason = model_attempt.get("reason")
                        bucket = attempted_by_model.setdefault(
                            model,
                            {"attempts": 0, "aider_successes": 0, "test_passes": 0},
                        )
                        bucket["attempts"] += 1
                        if model_attempt.get("success"):
                            bucket["aider_successes"] += 1
                            if attempt.get("tests_passed"):
                                bucket["test_passes"] += 1
                        if reason == "pre_screen_too_large":
                            oversized_context["pre_screen_skips"] += 1
                        elif reason == "request_too_large":
                            if (model_attempt.get("wall_seconds") or 0.0) > 0:
                                oversized_context["provider_rejections"] += 1
                            else:
                                oversized_context["fast_skips_after_rejection"] += 1
                else:
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

                post_check_reason = attempt.get("post_check_reason")
                if post_check_reason == "forbidden_file_edit":
                    subtype = attempt.get("forbidden_edit_subtype") or "generic"
                    forbidden_edit_attempts[subtype] = forbidden_edit_attempts.get(subtype, 0) + 1

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
        for entry in current_entries:
            fc = entry.get("failure_class")
            if fc:
                fail_classes[fc] = fail_classes.get(fc, 0) + 1
        if fail_classes:
            log.info("Failure classes:")
            for fc, n in sorted(fail_classes.items(), key=lambda x: -x[1]):
                log.info("  %s: %d", fc, n)
        if any(oversized_context.values()):
            log.info("Oversized context handling:")
            log.info("  Provider rejections: %d", oversized_context["provider_rejections"])
            log.info("  Fast skips after prior rejection: %d", oversized_context["fast_skips_after_rejection"])
            log.info("  Pre-screen skips: %d", oversized_context["pre_screen_skips"])
        if any(forbidden_edit_attempts.values()):
            log.info("Forbidden-edit retries:")
            log.info("  Generic drift: %d", forbidden_edit_attempts.get("generic", 0))
            log.info(
                "  Dependency-owned files: %d",
                forbidden_edit_attempts.get("dependency_target", 0),
            )

    breakdown = get_run_time_breakdown()
    total_wall = sum(breakdown.values())
    if total_wall > 0:
        log.info("\nTime breakdown:")
        for reason, seconds in sorted(breakdown.items(), key=lambda x: -x[1]):
            if seconds > 0:
                log.info("  %s: %.1fs", _time_bucket_label(reason), seconds)
        log.info("  Total aider wall time: %.1fs", total_wall)

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
        log.info("\nAfter Claude fixes each failure, re-run: make run")
        log.info("Use make resume only when the previous run was interrupted and you want to continue from the recorded attempt.")
        log.info("Fixed tasks are detected as passing and skipped automatically.")
    elif blocked:
        log.info("\n%s", "=" * 50)
        log.info("Some tasks were blocked by earlier dependency failures.")
        log.info("Fix the failed upstream tasks, then re-run the orchestrator.")
    else:
        log.info("\n%s", "=" * 50)
        log.info("All tasks passing -- ready to merge.")
        log.info("Open Claude Code and say: 'Review the task branches and merge them.'\n")

    if state and len(state.get("runs", [])) > 1:
        cumulative = {"passed": 0, "failed": 0, "skipped": 0, "blocked": 0}
        for run in state["runs"]:
            cumulative["passed"] += len(run.get("passed", []))
            cumulative["failed"] += len(run.get("failed", []))
            cumulative["skipped"] += len(run.get("skipped", []))
            cumulative["blocked"] += len(run.get("blocked", []))

        log.info("Cumulative session:")
        log.info("  Passed:  %d", cumulative["passed"])
        log.info("  Skipped: %d", cumulative["skipped"])
        log.info("  Blocked: %d", cumulative["blocked"])
        log.info("  Failed:  %d", cumulative["failed"])
