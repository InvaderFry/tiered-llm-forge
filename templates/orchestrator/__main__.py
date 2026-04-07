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
from .runner import run_tests, run_full_suite, file_size, check_regression
from .git_ops import (
    get_default_branch,
    ensure_default_branch_exists,
    branch_exists,
    branch_tip,
    checkout,
    delete_branch,
    merge_branch,
    revert_last_commit,
)
from .state import load_state, save_state, record_task
from .failure_class import classify as classify_failure

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


def _resolve_dependency_base(dependencies, default_branch):
    """
    Determine the start point for a new task branch based on its dependencies.

    - 0 deps   -> default branch
    - 1 dep    -> that dep's task branch (stacked)
    - N deps   -> default branch (caller will merge deps in afterwards)

    Returns (start_point, extra_merges) where ``extra_merges`` is the list
    of dependency branches the caller still needs to merge in after checkout.
    Any dependency whose branch does not exist is skipped with a warning.
    """
    dep_branches = []
    for dep in dependencies:
        dep_branch = f"task/{dep}"
        if branch_exists(dep_branch):
            dep_branches.append(dep_branch)
        else:
            print(
                f"  [dependency '{dep}' has no branch — falling back to default for that dep]"
            )

    if not dep_branches:
        return default_branch, []
    if len(dep_branches) == 1:
        return dep_branches[0], []
    # Multiple deps: start from default and merge each one in
    return default_branch, dep_branches


def run_task(spec, default_branch, state, specs_by_name=None):
    """
    Run a task through the full model escalation ladder.

    Re-run behaviour:
    - Branch exists + tests pass  -> skip (return 'skipped')
    - Branch exists + tests fail  -> flag for Claude review (return 'failed')
    - Branch does not exist       -> normal first run

    Dependency handling:
    The task branch is created from the tip of its dependency branch(es) so
    task N actually runs against task N-1's code. With multiple dependencies
    they are merged together onto a fresh branch rooted at ``default_branch``.
    """
    task_name = spec["task_name"]
    target_file = spec["target"]
    test_file = spec["test"]
    branch_name = f"task/{task_name}"
    dependencies = spec.get("dependencies", []) or []
    task_started = time.time()
    models_tried = []

    def _elapsed():
        return time.time() - task_started

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
            record_task(state, task_name, "skipped", duration_seconds=_elapsed())
            save_state(state)
            return "skipped"
        else:
            print(f"  Previously failed -- escalating to Claude review.")
            fail_log = SPECS_DIR / f"FAILED-{task_name}.log"
            fail_log.write_text(f"Previously attempted -- still failing on re-run.\n\n{output}")
            checkout(default_branch)
            record_task(
                state,
                task_name,
                "failed",
                attempts=0,
                duration_seconds=_elapsed(),
                failure_class=classify_failure(output),
            )
            save_state(state)
            return "failed"

    # --- Normal first run: branch from dependency tip(s) ---
    start_point, extra_merges = _resolve_dependency_base(dependencies, default_branch)
    base_sha = branch_tip(start_point)
    if start_point != default_branch or extra_merges:
        print(f"  Branching from '{start_point}' (deps: {', '.join(dependencies) or 'none'})")
    checkout(branch_name, create=True, start_point=start_point)

    for dep_branch in extra_merges:
        print(f"  Merging dependency branch {dep_branch} into {branch_name}")
        if not merge_branch(dep_branch, message=f"merge: {dep_branch} into {branch_name}"):
            fail_log = SPECS_DIR / f"FAILED-{task_name}.log"
            fail_log.write_text(
                f"Merge conflict while assembling dependencies for {task_name}.\n"
                f"Conflicting branch: {dep_branch}\n"
                "Resolve by running the tasks with fewer simultaneous dependencies, "
                "or fix the conflict manually and re-run the orchestrator.\n"
            )
            checkout(default_branch)
            record_task(
                state,
                task_name,
                "failed",
                attempts=0,
                duration_seconds=_elapsed(),
                failure_class="merge_conflict",
                base_branch=start_point,
                base_sha=base_sha,
            )
            save_state(state)
            return "failed"

    baseline_size = file_size(target_file)

    # Assemble read-only context for the implementer model:
    # the full spec file, the test file, and the target files of every
    # declared dependency. Missing paths are dropped inside run_aider.
    read_files = [str(spec["path"]), test_file]
    if specs_by_name:
        for dep_name in dependencies:
            dep_spec = specs_by_name.get(dep_name)
            if dep_spec and dep_spec.get("target"):
                read_files.append(dep_spec["target"])

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
            success, model_used = run_with_tier_fallback(
                "primary", spec_text, target_file, first_model, read_files=read_files,
            )
        else:
            _, test_output = run_tests(test_file)
            success, model_used = run_with_tier_fallback(
                "primary",
                f"Tests failed. Output:\n{_strip_urls(test_output)}\nFix the code to pass all tests.",
                target_file,
                first_model,
                read_files=read_files,
            )
        if model_used and model_used not in models_tried:
            models_tried.append(model_used)

        if check_regression(target_file, baseline_size):
            revert_last_commit(target_file, baseline_size)
            continue

        baseline_size = max(baseline_size, file_size(target_file))
        passed, _ = run_tests(test_file)
        if passed:
            print(f"PASSED (Stage 1, attempt {attempt}): {task_name}")
            checkout(default_branch)
            record_task(
                state,
                task_name,
                "passed",
                model=model_used,
                attempts=total_attempts,
                duration_seconds=_elapsed(),
                models_tried=models_tried,
                base_branch=start_point,
                base_sha=base_sha,
            )
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
            read_files=read_files,
        )
        if model_used and model_used not in models_tried:
            models_tried.append(model_used)

        if check_regression(target_file, baseline_size):
            revert_last_commit(target_file, baseline_size)
            continue

        baseline_size = max(baseline_size, file_size(target_file))
        passed, test_output = run_tests(test_file)
        if passed:
            print(f"PASSED (Escalation, attempt {attempt}): {task_name}")
            checkout(default_branch)
            record_task(
                state,
                task_name,
                "passed",
                model=model_used,
                attempts=total_attempts,
                duration_seconds=_elapsed(),
                models_tried=models_tried,
                base_branch=start_point,
                base_sha=base_sha,
            )
            save_state(state)
            return "passed"

    # --- Stage 3: Flag for Claude review ---
    _, test_output = run_tests(test_file)
    failure_label = classify_failure(test_output)
    fail_log = SPECS_DIR / f"FAILED-{task_name}.log"
    fail_log.write_text(
        f"Failed after primary tier ({primary_tier['retries']}x) "
        f"+ escalation ({escalation_tier['retries']}x).\n"
        f"Failure class: {failure_label}\n"
        f"Models tried: {', '.join(models_tried) or 'none'}\n\n"
        f"{test_output}"
    )
    print(f"ESCALATE TO CLAUDE: {task_name} (failure_class={failure_label})")
    checkout(default_branch)
    record_task(
        state,
        task_name,
        "failed",
        attempts=total_attempts,
        duration_seconds=_elapsed(),
        models_tried=models_tried,
        failure_class=failure_label,
        base_branch=start_point,
        base_sha=base_sha,
    )
    save_state(state)
    return "failed"


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
    from datetime import datetime, timezone
    import subprocess

    if not passed_task_names:
        print("\n[integration gate] No passing tasks — skipping.")
        return True

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    integration_branch = f"integration/run-{timestamp}"

    print(f"\n{'='*50}")
    print(f"INTEGRATION GATE: {integration_branch}")
    print(f"{'='*50}")

    # Record where we came from so we can always get back to it
    checkout(default_branch)
    checkout(integration_branch, create=True, start_point=default_branch)

    failed_merges = []
    for task_name in passed_task_names:
        branch = f"task/{task_name}"
        if not branch_exists(branch):
            print(f"  [skip] {branch} — branch missing")
            continue
        print(f"  merging {branch}")
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
        print(f"\n  INTEGRATION FAILED (merge conflict). See {log_path}")
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

    # Merges clean — run the full suite
    print("  running full test suite on integration branch...")
    passed, output = run_full_suite("tests")
    if not passed:
        log_path.write_text(
            f"Integration gate failed: full test suite failed on {integration_branch}.\n\n"
            f"{output}\n"
        )
        print(f"  INTEGRATION FAILED (test suite). See {log_path}")
        state["integration"] = {
            "branch": integration_branch,
            "status": "tests_failed",
            "timestamp": timestamp,
        }
        save_state(state)
        checkout(default_branch)
        # Keep the branch around so the human can reproduce the failure
        return False

    print(f"  INTEGRATION PASSED: {integration_branch} is ready to merge.")
    state["integration"] = {
        "branch": integration_branch,
        "status": "passed",
        "timestamp": timestamp,
    }
    save_state(state)
    checkout(default_branch)
    return True


def print_summary(results, default_branch, state=None):
    """Print final summary of pipeline run."""
    passed = results["passed"]
    failed = results["failed"]
    skipped = results["skipped"]

    print(f"\n{'='*50}")
    print(f"PASSED:  {len(passed)}")
    print(f"SKIPPED: {len(skipped)} (already passing)")
    print(f"FAILED:  {len(failed)}")

    # Observability roll-up: per-model success/failure counts and task timings.
    if state and state.get("tasks"):
        by_model = {}
        total_time = 0.0
        for name, entry in state["tasks"].items():
            dur = entry.get("duration_seconds") or 0.0
            total_time += dur
            model = entry.get("model") or "(none)"
            bucket = by_model.setdefault(model, {"passed": 0, "failed": 0, "skipped": 0})
            bucket[entry["status"]] = bucket.get(entry["status"], 0) + 1

        print(f"\nTotal task time: {total_time:.1f}s")
        print("By model:")
        for model, counts in sorted(by_model.items()):
            print(
                f"  {model}: {counts.get('passed', 0)} passed, "
                f"{counts.get('failed', 0)} failed, "
                f"{counts.get('skipped', 0)} skipped"
            )

        fail_classes = {}
        for entry in state["tasks"].values():
            fc = entry.get("failure_class")
            if fc:
                fail_classes[fc] = fail_classes.get(fc, 0) + 1
        if fail_classes:
            print("Failure classes:")
            for fc, n in sorted(fail_classes.items(), key=lambda x: -x[1]):
                print(f"  {fc}: {n}")

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

    specs_by_name = {load_spec(sf)["task_name"]: load_spec(sf) for sf in ordered}

    for i, sf in enumerate(ordered):
        spec = load_spec(sf)
        outcome = run_task(spec, default_branch, state, specs_by_name=specs_by_name)
        results[outcome].append(spec["task_name"])

        # Cooldown between tasks
        if i < len(ordered) - 1 and outcome != "skipped" and cooldown > 0:
            print(f"  [cooldown: sleeping {cooldown}s between tasks]")
            time.sleep(cooldown)

    print_summary(results, default_branch, state)

    # Integration gate: only run if every task succeeded (passed or skipped).
    # Failed tasks must be resolved before we try to assemble a merge.
    if not results["failed"]:
        all_task_names = [s["task_name"] for s in (load_spec(sf) for sf in ordered)]
        integration_gate(all_task_names, default_branch, state)
    else:
        print("\n[integration gate] Skipped — resolve failed tasks first.")

    save_state(state)


if __name__ == "__main__":
    main()
