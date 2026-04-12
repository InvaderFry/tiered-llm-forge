"""Pipeline run summary and observability roll-up."""

from . import FORGE_LOGS_DIR
from .log import get_logger

log = get_logger("summary")


def print_summary(results, default_branch, state=None):
    """Print final summary of pipeline run."""
    passed = results["passed"]
    failed = results["failed"]
    skipped = results["skipped"]

    log.info("\n%s", "=" * 50)
    log.info("PASSED:  %d", len(passed))
    log.info("SKIPPED: %d (already passing)", len(skipped))
    log.info("FAILED:  %d", len(failed))

    # Observability roll-up: per-model success/failure counts, task
    # timings, and token / cost totals scraped from aider's stdout.
    if state and state.get("tasks"):
        by_model = {}
        total_time = 0.0
        total_tokens_sent = 0
        total_tokens_received = 0
        total_cost = 0.0
        for name, entry in state["tasks"].items():
            dur = entry.get("duration_seconds") or 0.0
            total_time += dur
            total_tokens_sent += entry.get("tokens_sent", 0) or 0
            total_tokens_received += entry.get("tokens_received", 0) or 0
            total_cost += entry.get("cost_usd", 0.0) or 0.0
            model = entry.get("model") or "(none)"
            bucket = by_model.setdefault(
                model,
                {"passed": 0, "failed": 0, "skipped": 0,
                 "tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0},
            )
            bucket[entry["status"]] = bucket.get(entry["status"], 0) + 1
            bucket["tokens_sent"] += entry.get("tokens_sent", 0) or 0
            bucket["tokens_received"] += entry.get("tokens_received", 0) or 0
            bucket["cost_usd"] += entry.get("cost_usd", 0.0) or 0.0

        log.info("\nTotal task time: %.1fs", total_time)
        if total_tokens_sent or total_tokens_received or total_cost:
            log.info(
                "Total tokens: %s sent / %s received    Total cost: $%.4f",
                f"{total_tokens_sent:,}", f"{total_tokens_received:,}", total_cost,
            )
        log.info("By model:")
        for model, counts in sorted(by_model.items()):
            line = (
                f"  {model}: {counts.get('passed', 0)} passed, "
                f"{counts.get('failed', 0)} failed, "
                f"{counts.get('skipped', 0)} skipped"
            )
            if counts["tokens_sent"] or counts["cost_usd"]:
                line += (
                    f"  ({counts['tokens_sent']:,}+{counts['tokens_received']:,} tok, "
                    f"${counts['cost_usd']:.4f})"
                )
            log.info("%s", line)

        fail_classes = {}
        for entry in state["tasks"].values():
            fc = entry.get("failure_class")
            if fc:
                fail_classes[fc] = fail_classes.get(fc, 0) + 1
        if fail_classes:
            log.info("Failure classes:")
            for fc, n in sorted(fail_classes.items(), key=lambda x: -x[1]):
                log.info("  %s: %d", fc, n)

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
    else:
        log.info("\n%s", "=" * 50)
        log.info("All tasks passing -- ready to merge.")
        log.info("Open Claude Code and say: 'Review the task branches and merge them.'\n")
