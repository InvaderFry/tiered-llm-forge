"""CLI entry point for the orchestrator pipeline.

Usage:
    python3 -m orchestrator              # run the pipeline
    python3 -m orchestrator --validate   # validate specs only
    python3 -m orchestrator --dry-run    # preview without running models
"""

import argparse
import re
import sys
import time
from pathlib import Path

from .config import load_config, get_config, get_tier
from .spec_parser import load_spec, compress_spec, validate_specs, topological_sort
from .model_router import select_model_for_spec, run_aider, run_with_tier_fallback
from .runner import run_tests, file_size, check_regression
from .git_ops import (
    get_default_branch,
    ensure_default_branch_exists,
    branch_exists,
    checkout,
    revert_last_commit,
)
from .state import load_state, save_state, record_task

SPECS_DIR = Path(__file__).parent.parent / "specs"

_URL_RE = re.compile(r"https?://\S+")


def _strip_urls(text):
    """Remove URLs from test output so aider doesn't try to scrape them."""
    return _URL_RE.sub("[URL]", text)


def cmd_validate():
    """Validate all specs and print results."""
    errors, warnings = validate_specs(SPECS_DIR)

    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f"  ⚠  {w}")
        print()

    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  ✗  {e}")
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s). Fix errors before running.")
        return 1

    # Also show dependency order
    spec_files = sorted(SPECS_DIR.glob("task-*.md"))
    try:
        ordered = topological_sort(spec_files)
        print("Dependency order:")
        for i, sf in enumerate(ordered, 1):
            spec = load_spec(sf)
            deps = spec["dependencies"]
            dep_str = f" (after {', '.join(deps)})" if deps else ""
            print(f"  {i}. {spec['task_name']} -> {spec['target']}{dep_str}")
    except ValueError as e:
        print(f"  ✗  {e}")
        return 1

    print(f"\n0 errors, {len(warnings)} warning(s). Specs are valid.")
    return 0


def cmd_dry_run():
    """Preview what the pipeline would do without running models."""
    spec_files = sorted(SPECS_DIR.glob("task-*.md"))
    if not spec_files:
        print("No spec files found in specs/")
        return 0

    # Validate first
    errors, _ = validate_specs(SPECS_DIR)
    if errors:
        print("Cannot dry-run: specs have validation errors. Run --validate first.")
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

    print(f"{'='*50}")
    print(f"DRY RUN — {len(ordered)} tasks")
    print(f"Default branch: {default_branch}")
    print(f"Primary models: {', '.join(primary['models'])} ({primary['retries']} retries)")
    print(f"Escalation models: {', '.join(escalation['models'])} ({escalation['retries']} retries)")
    print(f"Cooldown: {cfg.get('cooldown_seconds', 30)}s between tasks")
    print(f"{'='*50}\n")

    for i, sf in enumerate(ordered, 1):
        spec = load_spec(sf)
        branch = f"task/{spec['task_name']}"
        status = "EXISTS" if branch_exists(branch) else "NEW"
        model = select_model_for_spec(spec["body"])
        compressed = compress_spec(spec["raw_text"], spec["task_name"])
        pct = int(len(compressed) / max(len(spec["raw_text"]), 1) * 100)

        print(f"  {i}. [{status}] {spec['task_name']}")
        print(f"     Target: {spec['target']}")
        print(f"     Test:   {spec['test']}")
        print(f"     Model:  {model}")
        print(f"     Spec:   {len(spec['raw_text'])} chars -> {len(compressed)} chars ({pct}%)")
        if spec["dependencies"]:
            print(f"     Deps:   {', '.join(spec['dependencies'])}")
        print()

    return 0


def run_task(spec, default_branch, state):
    """
    Run a task through the full model escalation ladder.

    Re-run behaviour:
    - Branch exists + tests pass  -> skip (return 'skipped')
    - Branch exists + tests fail  -> flag for Claude review (return 'failed')
    - Branch does not exist       -> normal first run
    """
    task_name = spec["task_name"]
    target_file = spec["target"]
    test_file = spec["test"]
    branch_name = f"task/{task_name}"

    print(f"\n{'='*50}\n{task_name}\n{'='*50}")

    # Compress spec
    spec_text = compress_spec(spec["raw_text"], task_name)

    # --- Handle re-run: branch already exists ---
    if branch_exists(branch_name):
        print(f"  Branch '{branch_name}' already exists -- checking previous result...")
        checkout(branch_name)
        passed, output = run_tests(test_file)
        if passed:
            print(f"  Already passing -- skipping.")
            checkout(default_branch)
            record_task(state, task_name, "skipped")
            save_state(state)
            return "skipped"
        else:
            print(f"  Previously failed -- escalating to Claude review.")
            fail_log = SPECS_DIR / f"FAILED-{task_name}.log"
            fail_log.write_text(f"Previously attempted -- still failing on re-run.\n\n{output}")
            checkout(default_branch)
            record_task(state, task_name, "failed", attempts=0)
            save_state(state)
            return "failed"

    # --- Normal first run ---
    checkout(branch_name, create=True)
    baseline_size = file_size(target_file)

    cfg = get_config()
    primary_tier = get_tier("primary")
    escalation_tier = get_tier("escalation")
    total_attempts = 0

    # --- Stage 1: Primary tier ---
    first_model = select_model_for_spec(spec_text)
    print(f"Stage 1: Primary tier ({primary_tier['retries']} attempts)")

    for attempt in range(1, primary_tier["retries"] + 1):
        total_attempts += 1
        print(f"  Attempt {attempt}/{primary_tier['retries']}...")

        if attempt == 1:
            success, model_used = run_with_tier_fallback("primary", spec_text, target_file, first_model)
        else:
            _, test_output = run_tests(test_file)
            success, model_used = run_with_tier_fallback(
                "primary",
                f"Tests failed. Output:\n{_strip_urls(test_output)}\nFix the code to pass all tests.",
                target_file,
                first_model,
            )

        if check_regression(target_file, baseline_size):
            revert_last_commit(target_file, baseline_size)
            continue

        baseline_size = max(baseline_size, file_size(target_file))
        passed, _ = run_tests(test_file)
        if passed:
            print(f"PASSED (Stage 1, attempt {attempt}): {task_name}")
            checkout(default_branch)
            record_task(state, task_name, "passed", model=model_used, attempts=total_attempts)
            save_state(state)
            return "passed"

    # --- Stage 2: Escalation tier ---
    print(f"Stage 2: Escalation ({escalation_tier['retries']} attempts)")

    for attempt in range(1, escalation_tier["retries"] + 1):
        total_attempts += 1
        print(f"  Attempt {attempt}/{escalation_tier['retries']}...")

        _, test_output = run_tests(test_file)
        success, model_used = run_with_tier_fallback(
            "escalation",
            f"Previous model failed. Tests output:\n{_strip_urls(test_output)}\nAnalyze carefully and fix.",
            target_file,
        )

        if check_regression(target_file, baseline_size):
            revert_last_commit(target_file, baseline_size)
            continue

        baseline_size = max(baseline_size, file_size(target_file))
        passed, test_output = run_tests(test_file)
        if passed:
            print(f"PASSED (Escalation, attempt {attempt}): {task_name}")
            checkout(default_branch)
            record_task(state, task_name, "passed", model=model_used, attempts=total_attempts)
            save_state(state)
            return "passed"

    # --- Stage 3: Flag for Claude review ---
    _, test_output = run_tests(test_file)
    fail_log = SPECS_DIR / f"FAILED-{task_name}.log"
    fail_log.write_text(
        f"Failed after primary tier ({primary_tier['retries']}x) "
        f"+ escalation ({escalation_tier['retries']}x).\n\n{test_output}"
    )
    print(f"ESCALATE TO CLAUDE: {task_name}")
    checkout(default_branch)
    record_task(state, task_name, "failed", attempts=total_attempts)
    save_state(state)
    return "failed"


def print_summary(results, default_branch):
    """Print final summary of pipeline run."""
    passed = results["passed"]
    failed = results["failed"]
    skipped = results["skipped"]

    print(f"\n{'='*50}")
    print(f"PASSED:  {len(passed)}")
    print(f"SKIPPED: {len(skipped)} (already passing)")
    print(f"FAILED:  {len(failed)}")

    if failed:
        print(f"\n{'='*50}")
        print("Claude review needed for these failures:")
        for f in failed:
            log = SPECS_DIR / f"FAILED-{f}.log"
            print(f"\n  Task:   {f}")
            print(f"  Branch: task/{f}")
            print(f"  Log:    {log}")
            print(f"  Prompt: > Fix {f}. The log is at {log}.")
        print(f"\nAfter Claude fixes each failure, re-run: python3 -m orchestrator")
        print("Fixed tasks are detected as passing and skipped automatically.")
    else:
        print(f"\n{'='*50}")
        print("All tasks passing -- ready to merge.")
        print("Open Claude Code and say: 'Review the task branches and merge them.'\n")


def main():
    parser = argparse.ArgumentParser(
        description="Tiered LLM orchestrator — run cheap models against spec files."
    )
    parser.add_argument("--validate", action="store_true", help="Validate specs without running")
    parser.add_argument("--dry-run", action="store_true", help="Preview pipeline without running models")
    parser.add_argument("--config", default="models.yaml", help="Path to models.yaml config")
    args = parser.parse_args()

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
        print("No spec files found in specs/. Add task-*.md files and re-run.")
        sys.exit(0)

    # Sort by dependencies if declared
    try:
        ordered = topological_sort(spec_files)
    except ValueError as e:
        print(f"ERROR: {e}")
        print("Fix dependency cycles before running. Use --validate to check.")
        sys.exit(1)

    cfg = get_config()
    cooldown = cfg.get("cooldown_seconds", 30)

    state = load_state()
    results = {"passed": [], "failed": [], "skipped": []}

    for i, sf in enumerate(ordered):
        spec = load_spec(sf)
        outcome = run_task(spec, default_branch, state)
        results[outcome].append(spec["task_name"])

        # Cooldown between tasks
        if i < len(ordered) - 1 and outcome != "skipped" and cooldown > 0:
            print(f"  [cooldown: sleeping {cooldown}s between tasks]")
            time.sleep(cooldown)

    print_summary(results, default_branch)
    save_state(state)


if __name__ == "__main__":
    main()
