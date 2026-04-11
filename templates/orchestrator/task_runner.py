"""Per-task orchestration: model escalation, dependency resolution, regression guard."""

import re
import time
from datetime import datetime

from . import FORGE_LOGS_DIR
from .log import get_logger

log = get_logger("task_runner")
from .config import get_config, get_tier
from .model_router import (
    select_model_for_spec,
    run_with_tier_fallback,
    is_request_too_large,
    clear_request_too_large,
)
from .runner import run_tests, file_size, check_regression
from .git_ops import (
    branch_exists,
    branch_tip,
    checkout,
    merge_branch,
    revert_last_commit,
)
from .state import save_state, record_task, record_attempt, get_resume_point
from .failure_class import classify as classify_failure

_URL_RE = re.compile(r"https?://\S+")


def _strip_urls(text):
    """Remove URLs from test output so aider doesn't try to scrape them."""
    return _URL_RE.sub("[URL]", text)


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
            log.warning("  [dependency '%s' has no branch -- falling back to default for that dep]", dep)

    if not dep_branches:
        return default_branch, []
    if len(dep_branches) == 1:
        return dep_branches[0], []
    # Multiple deps: start from default and merge each one in
    return default_branch, dep_branches


def run_task(spec, default_branch, state, specs_by_name=None, resume=False,
             cwd=None, branch_preexisted=None):
    """
    Run a task through the full model escalation ladder.

    Re-run behaviour:
    - Branch exists + tests pass  -> skip (return 'skipped')
    - Branch exists + tests fail + resume=False -> flag for Claude review
    - Branch exists + tests fail + resume=True  -> resume from last attempt
    - Branch does not exist       -> normal first run

    Dependency handling:
    The task branch is created from the tip of its dependency branch(es) so
    task N actually runs against task N-1's code. With multiple dependencies
    they are merged together onto a fresh branch rooted at ``default_branch``.

    When ``cwd`` is set (worktree mode), branch creation and checkout are
    skipped — the caller is responsible for setting up the worktree on the
    correct branch. All subprocess calls run inside ``cwd``.

    ``branch_preexisted`` tells worktree mode whether the branch existed
    before the worktree was created (since worktree creation itself creates
    the branch). When None, falls back to ``branch_exists()``.
    """
    task_name = spec["task_name"]
    target_file = spec["target"]
    test_file = spec["test"]
    branch_name = f"task/{task_name}"
    dependencies = spec.get("dependencies", []) or []
    task_started = time.time()
    models_tried = []
    worktree = cwd is not None

    # Request-too-large is a per-task property, not session-wide: a task with
    # a tiny spec should not inherit "skip qwen3" from an earlier huge task.
    clear_request_too_large()

    def _elapsed():
        return time.time() - task_started

    log.info("\n%s\n%s\n%s", "=" * 50, task_name, "=" * 50)

    implement_message = (
        f"Implement the task described in {spec['path']}. "
        f"All requirements (function signatures, types, constraints) live in "
        f"that file. Edit {target_file} so that the tests in {test_file} pass. "
        f"Do not modify the test file."
    )

    # --- Handle re-run: branch already exists ---
    # In worktree mode the caller tells us whether the branch pre-existed
    # (worktree creation itself creates the branch as a side effect).
    branch_was_preexisting = branch_preexisted if worktree else branch_exists(branch_name, cwd=cwd)
    if branch_was_preexisting:
        log.info("  Branch '%s' already exists -- checking previous result...", branch_name)
        if not worktree:
            checkout(branch_name)
        passed, output = run_tests(test_file, cwd=cwd)
        if passed:
            log.info("  Already passing -- skipping.")
            if not worktree:
                checkout(default_branch)
            record_task(state, task_name, "skipped", duration_seconds=_elapsed())
            save_state(state)
            return "skipped"

        # Check if we should resume from where we left off
        resume_point = get_resume_point(state, task_name) if resume else None
        if resume_point:
            log.info(
                "  Resuming from %s tier, attempt %d (total prior attempts: %d)",
                resume_point["tier"], resume_point["attempt"] + 1,
                resume_point["total_attempts"],
            )
            # Stay on the branch — fall through to the retry loop below
        else:
            log.info("  Previously failed -- escalating to Claude review.")
            FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
            _ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            fail_log = FORGE_LOGS_DIR / f"FAILED-{task_name}-{_ts}.log"
            fail_log.write_text(f"Previously attempted -- still failing on re-run.\n\n{output}")
            if not worktree:
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
    else:
        resume_point = None

    # --- Normal first run (or resume): branch from dependency tip(s) ---
    if not resume_point:
        start_point, extra_merges = _resolve_dependency_base(dependencies, default_branch)
    else:
        # Resuming — branch already exists, no need to re-create
        start_point = default_branch
        extra_merges = []

    if not resume_point:
        base_sha = branch_tip(start_point, cwd=cwd)
        if not worktree:
            if start_point != default_branch or extra_merges:
                log.info("  Branching from '%s' (deps: %s)", start_point, ", ".join(dependencies) or "none")
            checkout(branch_name, create=True, start_point=start_point)

        for dep_branch in extra_merges:
            log.info("  Merging dependency branch %s into %s", dep_branch, branch_name)
            if not merge_branch(dep_branch, message=f"merge: {dep_branch} into {branch_name}", cwd=cwd):
                FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
                _ts = datetime.now().strftime("%Y%m%dT%H%M%S")
                fail_log = FORGE_LOGS_DIR / f"FAILED-{task_name}-{_ts}.log"
                fail_log.write_text(
                    f"Merge conflict while assembling dependencies for {task_name}.\n"
                    f"Conflicting branch: {dep_branch}\n"
                    "Resolve by running the tasks with fewer simultaneous dependencies, "
                    "or fix the conflict manually and re-run the orchestrator.\n"
                )
                if not worktree:
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
    else:
        base_sha = branch_tip(branch_name, cwd=cwd)

    baseline_size = file_size(target_file, cwd=cwd)

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
    total_attempts = resume_point["total_attempts"] if resume_point else 0
    task_stats = {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0}
    llm_fail_reasons: list = []  # LLM-level failure reasons (e.g. "request_too_large")

    # On resume, determine where to start: skip tiers/attempts already done
    skip_primary = False
    primary_start = 1
    escalation_start = 1
    if resume_point:
        if resume_point["tier"] == "escalation":
            skip_primary = True
            escalation_start = resume_point["attempt"] + 1
        else:
            primary_start = resume_point["attempt"] + 1

    def _accumulate(stats):
        if not stats:
            return
        task_stats["tokens_sent"] += stats.get("tokens_sent", 0)
        task_stats["tokens_received"] += stats.get("tokens_received", 0)
        task_stats["cost_usd"] += stats.get("cost_usd", 0.0)

    # --- Stage 1: Primary tier ---
    first_model = select_model_for_spec(spec["body"])
    if not skip_primary:
        log.info("Stage 1: Primary tier (%d attempts, starting at %d)", primary_tier["retries"], primary_start)

        for attempt in range(primary_start, primary_tier["retries"] + 1):
            total_attempts += 1
            log.info("  Attempt %d/%d...", attempt, primary_tier["retries"])

            head_before = branch_tip("HEAD", cwd=cwd)
            if attempt == 1 and not resume_point:
                success, model_used, stats = run_with_tier_fallback(
                    "primary", implement_message, target_file, first_model,
                    read_files=read_files, cwd=cwd,
                )
            else:
                _, test_output = run_tests(test_file, cwd=cwd)
                success, model_used, stats = run_with_tier_fallback(
                    "primary",
                    f"Tests failed. Output:\n{_strip_urls(test_output)}\nFix the code to pass all tests.",
                    target_file,
                    first_model,
                    read_files=read_files,
                    cwd=cwd,
                )
            _accumulate(stats)
            if model_used and model_used not in models_tried:
                models_tried.append(model_used)
            if not success and not model_used:
                # All models in the tier failed without writing anything —
                # record why so the Stage 3 log reflects the real cause.
                primary_models = get_tier("primary")["models"]
                if all(is_request_too_large(m) for m in primary_models):
                    if "request_too_large" not in llm_fail_reasons:
                        llm_fail_reasons.append("request_too_large")

            head_after = branch_tip("HEAD", cwd=cwd)
            head_moved = bool(head_after) and head_after != head_before
            if head_moved and check_regression(target_file, baseline_size, cwd=cwd):
                record_attempt(state, task_name, attempt, "primary", model_used, success, tests_passed=False)
                save_state(state)
                revert_last_commit(target_file, baseline_size, cwd=cwd)
                continue
            if not head_moved and not success:
                # Aider never committed — nothing to revert, but also nothing
                # to test against. Move to the next attempt without nuking
                # dependency history with git reset --hard HEAD~1.
                log.info("  [no commit produced by aider -- nothing to revert]")
                record_attempt(state, task_name, attempt, "primary", model_used, success, tests_passed=False)
                save_state(state)
                continue

            baseline_size = max(baseline_size, file_size(target_file, cwd=cwd))
            passed, _ = run_tests(test_file, cwd=cwd)

            record_attempt(state, task_name, attempt, "primary", model_used, success, tests_passed=passed)
            save_state(state)

            if passed:
                log.info("PASSED (Stage 1, attempt %d): %s", attempt, task_name)
                if not worktree:
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
                    tokens_sent=task_stats["tokens_sent"],
                    tokens_received=task_stats["tokens_received"],
                    cost_usd=task_stats["cost_usd"],
                )
                save_state(state)
                return "passed"

    # --- Stage 2: Escalation tier ---
    log.info("Stage 2: Escalation (%d attempts, starting at %d)", escalation_tier["retries"], escalation_start)

    for attempt in range(escalation_start, escalation_tier["retries"] + 1):
        total_attempts += 1
        log.info("  Attempt %d/%d...", attempt, escalation_tier["retries"])

        _, test_output = run_tests(test_file, cwd=cwd)
        head_before = branch_tip("HEAD", cwd=cwd)
        success, model_used, stats = run_with_tier_fallback(
            "escalation",
            f"Previous model failed. Tests output:\n{_strip_urls(test_output)}\nAnalyze carefully and fix.",
            target_file,
            read_files=read_files,
            cwd=cwd,
        )
        _accumulate(stats)
        if model_used and model_used not in models_tried:
            models_tried.append(model_used)

        head_after = branch_tip("HEAD", cwd=cwd)
        head_moved = bool(head_after) and head_after != head_before
        if head_moved and check_regression(target_file, baseline_size, cwd=cwd):
            record_attempt(state, task_name, attempt, "escalation", model_used, success, tests_passed=False)
            save_state(state)
            revert_last_commit(target_file, baseline_size, cwd=cwd)
            continue
        if not head_moved and not success:
            log.info("  [no commit produced by aider -- nothing to revert]")
            record_attempt(state, task_name, attempt, "escalation", model_used, success, tests_passed=False)
            save_state(state)
            continue

        baseline_size = max(baseline_size, file_size(target_file, cwd=cwd))
        passed, test_output = run_tests(test_file, cwd=cwd)

        record_attempt(state, task_name, attempt, "escalation", model_used, success, tests_passed=passed)
        save_state(state)

        if passed:
            log.info("PASSED (Escalation, attempt %d): %s", attempt, task_name)
            if not worktree:
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
                tokens_sent=task_stats["tokens_sent"],
                tokens_received=task_stats["tokens_received"],
                cost_usd=task_stats["cost_usd"],
            )
            save_state(state)
            return "passed"

    # --- Stage 3: Flag for Claude review ---
    _, test_output = run_tests(test_file, cwd=cwd)
    failure_label = classify_failure(test_output)
    FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    fail_log = FORGE_LOGS_DIR / f"FAILED-{task_name}-{_ts}.log"
    llm_context = (
        f"LLM failure reason: {', '.join(llm_fail_reasons)}\n" if llm_fail_reasons else ""
    )
    fail_log.write_text(
        f"Failed after primary tier ({primary_tier['retries']}x) "
        f"+ escalation ({escalation_tier['retries']}x).\n"
        f"Failure class: {failure_label}\n"
        f"{llm_context}"
        f"Models tried: {', '.join(models_tried) or 'none'}\n"
        f"Tokens (sent/received): {task_stats['tokens_sent']} / {task_stats['tokens_received']}\n"
        f"Cost: ${task_stats['cost_usd']:.4f}\n\n"
        f"{test_output}"
    )
    log.warning("ESCALATE TO CLAUDE: %s (failure_class=%s)", task_name, failure_label)
    if not worktree:
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
        tokens_sent=task_stats["tokens_sent"],
        tokens_received=task_stats["tokens_received"],
        cost_usd=task_stats["cost_usd"],
    )
    save_state(state)
    return "failed"
