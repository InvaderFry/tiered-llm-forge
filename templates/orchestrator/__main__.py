"""CLI entry point for the orchestrator pipeline.

Usage:
    python3 -m orchestrator              # run the pipeline
    python3 -m orchestrator --validate   # validate specs only
    python3 -m orchestrator --dry-run    # preview without running models
"""

import argparse
import sys
import time

from . import SPECS_DIR
from .config import load_config, get_config, get_tier
from .log import setup_logging, get_logger
from .spec_parser import load_spec, validate_specs, topological_sort
from .model_router import select_model_for_spec
from .git_ops import get_default_branch, ensure_default_branch_exists, branch_exists
from .state import load_state, save_state
from .task_runner import run_task
from .integration import integration_gate
from .parallel import find_parallel_groups, run_parallel_group
from .summary import print_summary

log = get_logger("cli")


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
    log.info("Cooldown: %ds between tasks", cfg.get("cooldown_seconds", 30))
    log.info("=" * 50 + "\n")

    for i, sf in enumerate(ordered, 1):
        spec = load_spec(sf)
        branch = f"task/{spec['task_name']}"
        status = "EXISTS" if branch_exists(branch) else "NEW"
        model = select_model_for_spec(spec["body"])

        log.info("  %d. [%s] %s", i, status, spec["task_name"])
        log.info("     Target: %s", spec["target"])
        log.info("     Test:   %s", spec["test"])
        log.info("     Model:  %s", model)
        log.info("     Spec:   %d chars (attached to aider as --read)", len(spec["raw_text"]))
        if spec["dependencies"]:
            log.info("     Deps:   %s", ", ".join(spec["dependencies"]))
        log.info("")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Tiered LLM orchestrator — run cheap models against spec files."
    )
    parser.add_argument("--validate", action="store_true", help="Validate specs without running")
    parser.add_argument("--dry-run", action="store_true", help="Preview pipeline without running models")
    parser.add_argument("--config", default="models.yaml", help="Path to models.yaml config")
    parser.add_argument("--resume", action="store_true", help="Resume failed tasks from last attempt instead of flagging for review")
    parser.add_argument("--parallel", action="store_true",
                        help="Group independent tasks into dependency waves")
    parser.add_argument("--verbose", action="store_true", help="Enable debug-level output with timestamps")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)
    load_config(args.config)

    if args.validate:
        sys.exit(cmd_validate())

    if args.dry_run:
        sys.exit(cmd_dry_run())

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

    state = load_state()
    results = {"passed": [], "failed": [], "skipped": []}

    specs_by_name = {}
    for sf in ordered:
        _spec = load_spec(sf)
        specs_by_name[_spec["task_name"]] = _spec

    ordered_specs = [load_spec(sf) for sf in ordered]

    if args.parallel:
        # Wave mode: group independent tasks and run each wave
        groups = find_parallel_groups(ordered_specs)
        log.info("Wave mode: %d wave(s) across %d tasks", len(groups), len(ordered_specs))
        for wave_i, group in enumerate(groups, 1):
            log.info("\n--- Wave %d: %d task(s) ---", wave_i, len(group))
            wave_results = run_parallel_group(
                group, default_branch, state, run_task,
                specs_by_name=specs_by_name, resume=args.resume,
            )
            for task_name, outcome in wave_results.items():
                results[outcome].append(task_name)

            # Cooldown between waves
            if wave_i < len(groups) and cooldown > 0:
                log.info("  [cooldown: sleeping %ds between waves]", cooldown)
                time.sleep(cooldown)
    else:
        # Sequential mode (default)
        for i, spec in enumerate(ordered_specs):
            outcome = run_task(spec, default_branch, state, specs_by_name=specs_by_name, resume=args.resume)
            results[outcome].append(spec["task_name"])

            # Cooldown between tasks
            if i < len(ordered_specs) - 1 and outcome != "skipped" and cooldown > 0:
                log.info("  [cooldown: sleeping %ds between tasks]", cooldown)
                time.sleep(cooldown)

    print_summary(results, default_branch, state)

    # Integration gate: only run if every task succeeded (passed or skipped).
    # Failed tasks must be resolved before we try to assemble a merge.
    if not results["failed"]:
        all_task_names = [s["task_name"] for s in ordered_specs]
        integration_gate(all_task_names, default_branch, state)
    else:
        log.info("\n[integration gate] Skipped -- resolve failed tasks first.")

    save_state(state)


if __name__ == "__main__":
    main()
