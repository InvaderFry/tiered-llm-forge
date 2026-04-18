"""CLI entry point for the orchestrator pipeline.

Usage:
    python3 -m orchestrator              # run the pipeline
    python3 -m orchestrator --validate   # validate specs only
    python3 -m orchestrator --dry-run    # preview without running models
"""

import argparse
import sys
import time
from pathlib import Path

from . import SPECS_DIR
from .config import load_config, get_auto_parallel, get_config, get_tier
from .log import setup_logging, get_logger
from .model_router import adaptive_cooldown_seconds
from .preflight import run_startup_preflight
from .spec_parser import load_spec, validate_specs, topological_sort
from .git_ops import (
    get_default_branch,
    ensure_default_branch_exists,
    branch_exists,
    validate_tracked_clean,
)
from .state import load_state, save_state, append_run_summary, record_task
from .task_runner import run_task, reset_run_time_breakdown
from .integration import integration_gate
from .parallel import find_parallel_groups, run_parallel_group
from .summary import print_summary

log = get_logger("cli")


def _blocked_dependencies(spec, outcomes):
    """Return failed/blocked dependencies for *spec* based on prior outcomes."""
    return [
        dep for dep in (spec.get("dependencies", []) or [])
        if outcomes.get(dep) in {"failed", "blocked"}
    ]


def _spec_test_input_paths(ordered_specs):
    """Return repo-relative spec and test paths for git preflight checks."""
    return [str(s["path"]) for s in ordered_specs] + [s["test"] for s in ordered_specs]


def _log_preflight_messages(errors, warnings):
    """Log startup preflight output in a consistent format."""
    for warning in warnings:
        log.warning("Preflight warning: %s", warning)
    for error in errors:
        log.error("Preflight error: %s", error)


def _log_tracked_clean_errors(action, errors):
    """Log a consistent, actionable preflight failure message."""
    log.error(
        "Cannot %s: task specs/tests must be committed and clean before orchestration.",
        action,
    )
    for err in errors:
        log.error("  ✗  %s", err)
    log.error("Commit planner inputs before retrying:")
    log.error("  git add specs tests")
    log.error('  git commit -m "chore: add task specs and tests"')


def cmd_validate():
    """Validate all specs and print results."""
    errors, warnings = validate_specs(SPECS_DIR)

    if warnings:
        log.warning("WARNINGS:")
        for w in warnings:
            log.warning("  ⚠  %s", w)
        log.info("")

    if errors:
        log.error("ERRORS:")
        for e in errors:
            log.error("  ✗  %s", e)
        log.error("%d error(s), %d warning(s). Fix errors before running.", len(errors), len(warnings))
        return 1

    # Also show dependency order
    spec_files = sorted(SPECS_DIR.glob("task-*.md"))
    try:
        ordered = topological_sort(spec_files)
        log.info("Dependency order:")
        for i, sf in enumerate(ordered, 1):
            spec = load_spec(sf)
            deps = spec["dependencies"]
            dep_str = f" (after {', '.join(deps)})" if deps else ""
            log.info("  %d. %s -> %s%s", i, spec["task_name"], spec["target"], dep_str)
    except ValueError as e:
        log.error("  ✗  %s", e)
        return 1

    log.info("0 errors, %d warning(s). Specs are valid.", len(warnings))
    return 0


def cmd_dry_run():
    """Preview what the pipeline would do without running models."""
    spec_files = sorted(SPECS_DIR.glob("task-*.md"))
    if not spec_files:
        log.info("No spec files found in specs/")
        return 0

    # Validate first
    errors, _ = validate_specs(SPECS_DIR)
    if errors:
        log.error("Cannot dry-run: specs have validation errors. Run --validate first.")
        return 1

    try:
        ordered = topological_sort(spec_files)
    except ValueError:
        ordered = spec_files

    ordered_specs = [load_spec(sf) for sf in ordered]
    preflight_errors = validate_tracked_clean(_spec_test_input_paths(ordered_specs))
    if preflight_errors:
        _log_tracked_clean_errors("dry-run", preflight_errors)
        return 1

    startup_errors, startup_warnings = run_startup_preflight(repo_root=Path.cwd())
    _log_preflight_messages(startup_errors, startup_warnings)
    if startup_errors:
        log.error("Cannot dry-run until preflight errors are fixed.")
        return 1

    ensure_default_branch_exists()
    default_branch = get_default_branch()
    cfg = get_config()
    primary = get_tier("primary")
    escalation = get_tier("escalation")

    log.info("=" * 50)
    log.info("DRY RUN — %d tasks", len(ordered))
    log.info("Default branch: %s", default_branch)
    log.info("Primary models: %s (%d retries)", ", ".join(primary["models"]), primary["retries"])
    log.info("Escalation models: %s (%d retries)", ", ".join(escalation["models"]), escalation["retries"])
    log.info("Cooldown: up to %ds between tasks when provider pressure is detected", cfg.get("cooldown_seconds", 30))
    log.info("=" * 50 + "\n")

    for i, spec in enumerate(ordered_specs, 1):
        branch = f"task/{spec['task_name']}"
        status = "EXISTS" if branch_exists(branch) else "NEW"
        model = primary["models"][0]

        log.info("  %d. [%s] %s", i, status, spec["task_name"])
        log.info("     Target: %s", spec["target"])
        log.info("     Test:   %s", spec["test"])
        log.info("     Model:  %s", model)
        log.info("     Spec:   %d chars (attached to aider as --read)", len(spec["raw_text"]))
        if spec["dependencies"]:
            log.info("     Deps:   %s", ", ".join(spec["dependencies"]))
        log.info("")

    return 0


def cmd_preflight():
    """Validate config and runtime prerequisites without running the pipeline."""
    errors, warnings = run_startup_preflight(repo_root=Path.cwd())
    _log_preflight_messages(errors, warnings)
    if errors:
        log.error("Preflight failed. Fix the errors above before running the pipeline.")
        return 1

    log.info("Preflight passed. Config and provider env look ready.")
    return 0


def _cooldown_duration(max_cooldown):
    """Return the adaptive cooldown to apply before the next task or wave."""
    return adaptive_cooldown_seconds(max_cooldown)


def _should_cooldown(outcomes, max_cooldown):
    """Return True if adaptive provider-pressure signals warrant a cooldown."""
    del outcomes
    return _cooldown_duration(max_cooldown) > 0


def _run_parallel_schedule(groups, default_branch, state, run_task_fn, specs_by_name,
                           resume, max_workers, cooldown, results, outcomes):
    """Run dependency waves in parallel and update shared result trackers."""
    log.info("Wave mode: %d wave(s) across %d tasks (max workers: %d)",
             len(groups), len(specs_by_name), max_workers)
    for wave_i, group in enumerate(groups, 1):
        log.info("\n--- Wave %d: %d task(s) ---", wave_i, len(group))
        runnable = []
        for spec in group:
            blocked_by = _blocked_dependencies(spec, outcomes)
            if blocked_by:
                task_name = spec["task_name"]
                log.info(
                    "  [blocked] %s -- dependency failure in %s",
                    task_name, ", ".join(blocked_by),
                )
                record_task(
                    state,
                    task_name,
                    "blocked",
                    duration_seconds=0,
                    failure_class="dependency_failed",
                    blocked_by=blocked_by,
                )
                save_state(state)
                results["blocked"].append(task_name)
                outcomes[task_name] = "blocked"
            else:
                runnable.append(spec)

        if runnable:
            wave_results = run_parallel_group(
                runnable, default_branch, state, run_task_fn,
                specs_by_name=specs_by_name, resume=resume,
                max_workers=max_workers,
            )
        else:
            wave_results = {}
        for task_name, outcome in wave_results.items():
            results[outcome].append(task_name)
            outcomes[task_name] = outcome

        cooldown_wait = _cooldown_duration(cooldown)
        if wave_i < len(groups) and cooldown_wait > 0 and _should_cooldown(wave_results.values(), cooldown):
            log.info("  [cooldown: sleeping %.1fs between waves]", cooldown_wait)
            time.sleep(cooldown_wait)


def main():
    parser = argparse.ArgumentParser(
        description="Tiered LLM orchestrator — run cheap models against spec files."
    )
    parser.add_argument("--validate", action="store_true", help="Validate specs without running")
    parser.add_argument("--dry-run", action="store_true", help="Preview pipeline without running models")
    parser.add_argument("--preflight", action="store_true", help="Validate config and runtime prerequisites")
    parser.add_argument("--config", default="models.yaml", help="Path to models.yaml config")
    parser.add_argument("--resume", action="store_true", help="Resume failed tasks from last attempt instead of flagging for review")
    parser.add_argument("--parallel", nargs="?", type=int, const=4, default=None,
                        metavar="N",
                        help="Run independent tasks concurrently in waves (N = max workers, default 4)")
    auto_parallel_group = parser.add_mutually_exclusive_group()
    auto_parallel_group.add_argument(
        "--auto-parallel",
        dest="auto_parallel",
        action="store_true",
        help="Automatically switch to wave mode when the dependency graph has independent tasks",
    )
    auto_parallel_group.add_argument(
        "--no-auto-parallel",
        dest="auto_parallel",
        action="store_false",
        help="Force sequential mode even if models.yaml enables auto_parallel",
    )
    parser.set_defaults(auto_parallel=None)
    parser.add_argument("--verbose", action="store_true", help="Enable debug-level output with timestamps")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)
    load_config(args.config)

    if args.validate:
        sys.exit(cmd_validate())

    if args.dry_run:
        sys.exit(cmd_dry_run())

    if args.preflight:
        sys.exit(cmd_preflight())

    # --- Full pipeline run ---
    ensure_default_branch_exists()
    default_branch = get_default_branch()
    spec_files = sorted(SPECS_DIR.glob("task-*.md"))

    if not spec_files:
        log.info("No spec files found in specs/. Add task-*.md files and re-run.")
        sys.exit(0)

    # Sort by dependencies if declared
    try:
        ordered = topological_sort(spec_files)
    except ValueError as e:
        log.error("ERROR: %s", e)
        log.error("Fix dependency cycles before running. Use --validate to check.")
        sys.exit(1)

    cfg = get_config()
    cooldown = cfg.get("cooldown_seconds", 30)
    auto_parallel_enabled = (
        args.auto_parallel
        if args.auto_parallel is not None
        else get_auto_parallel()
    )

    preflight_errors, preflight_warnings = run_startup_preflight(repo_root=Path.cwd())
    _log_preflight_messages(preflight_errors, preflight_warnings)
    if preflight_errors:
        log.error("Cannot run the pipeline until preflight errors are fixed.")
        sys.exit(1)

    ordered_specs = [load_spec(sf) for sf in ordered]
    specs_by_name = {s["task_name"]: s for s in ordered_specs}
    preflight_errors = validate_tracked_clean(_spec_test_input_paths(ordered_specs))
    if preflight_errors:
        _log_tracked_clean_errors("run", preflight_errors)
        sys.exit(1)

    state = load_state()
    reset_run_time_breakdown()
    results = {"passed": [], "failed": [], "skipped": [], "blocked": []}

    outcomes = {}

    seq_groups = find_parallel_groups(ordered_specs)
    max_wave = max((len(g) for g in seq_groups), default=0)
    explicit_parallel = args.parallel is not None
    auto_parallel_active = (
        not explicit_parallel
        and auto_parallel_enabled
        and max_wave > 1
    )

    if explicit_parallel or auto_parallel_active:
        max_workers = args.parallel if explicit_parallel else 4
        if auto_parallel_active:
            log.info(
                "Auto-parallel: switching to parallel mode -- wave of %d independent tasks detected.",
                max_wave,
            )
        _run_parallel_schedule(
            seq_groups,
            default_branch,
            state,
            run_task,
            specs_by_name,
            args.resume,
            max_workers,
            cooldown,
            results,
            outcomes,
        )
    else:
        # Sequential mode (default). If the dependency graph has any wave
        # wider than one task, suggest --parallel once up-front so the user
        # knows the throughput win exists.
        if auto_parallel_enabled and max_wave <= 1:
            log.info("Auto-parallel enabled, but the dependency graph is fully serial -- staying sequential.")
        if max_wave > 1:
            log.info(
                "Hint: dependency graph has a wave of %d independent tasks — "
                "use 'make parallel' or --parallel to run them concurrently.",
                max_wave,
            )
        for i, spec in enumerate(ordered_specs):
            blocked_by = _blocked_dependencies(spec, outcomes)
            if blocked_by:
                task_name = spec["task_name"]
                log.info("  [blocked] %s -- dependency failure in %s", task_name, ", ".join(blocked_by))
                record_task(
                    state,
                    task_name,
                    "blocked",
                    duration_seconds=0,
                    failure_class="dependency_failed",
                    blocked_by=blocked_by,
                )
                save_state(state)
                results["blocked"].append(task_name)
                outcomes[task_name] = "blocked"
                continue

            outcome = run_task(spec, default_branch, state, specs_by_name=specs_by_name, resume=args.resume)
            results[outcome].append(spec["task_name"])
            outcomes[spec["task_name"]] = outcome

            # Cooldown between tasks
            cooldown_wait = _cooldown_duration(cooldown)
            if i < len(ordered_specs) - 1 and cooldown_wait > 0 and _should_cooldown([outcome], cooldown):
                log.info("  [cooldown: sleeping %.1fs between tasks]", cooldown_wait)
                time.sleep(cooldown_wait)

    print_summary(results, default_branch, state)

    # Append a lightweight run summary before the integration gate so the
    # history is recorded even if the gate fails or is skipped.
    append_run_summary(state, results)

    # Integration gate: only run if every task succeeded (passed or skipped).
    # Failed tasks must be resolved before we try to assemble a merge.
    if not results["failed"] and not results["blocked"]:
        all_task_names = [s["task_name"] for s in ordered_specs]
        integration_gate(all_task_names, default_branch, state)
    else:
        log.info("\n[integration gate] Skipped -- resolve failed/blocked tasks first.")

    save_state(state)


if __name__ == "__main__":
    main()
