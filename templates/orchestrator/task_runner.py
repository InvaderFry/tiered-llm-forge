"""Per-task orchestration: model escalation, dependency resolution, regression guard."""

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import FORGE_LOGS_DIR
from .config import get_config, get_tier
from .failure_class import classify as classify_failure, classify_terminal
from .git_ops import (
    branch_exists,
    branch_tip,
    changed_files_between,
    checkout,
    merge_branch,
    resolve_dependency_base,
    GIT_TIMEOUT,
)
from .log import get_logger, reserve_log_path, write_timestamped_log
from .model_router import (
    run_with_tier_fallback,
    is_request_too_large,
    is_invalid_model,
    clear_request_too_large,
    all_gemini_quota_exhausted,
)
from .runner import run_tests, file_size, check_regression
from .state import save_state, record_task, record_attempt, get_resume_point

log = get_logger("task_runner")

_URL_RE = re.compile(r"https?://\S+")


def _strip_urls(text):
    """Remove URLs from test output so aider doesn't try to scrape them."""
    return _URL_RE.sub("[URL]", text)


def revert_last_commit(target_file, baseline_size, cwd=None, reason=None):
    """Undo the last commit and log a warning about the regression.

    Lives here (rather than git_ops.py) because it needs ``file_size`` from
    runner.py; keeping it in git_ops.py would create a runner↔git_ops cycle.
    """
    if reason:
        log.warning("  %s", reason)
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True, timeout=GIT_TIMEOUT, cwd=cwd)
        return

    current = file_size(target_file, cwd=cwd)
    if baseline_size > 0:
        pct = int((1 - current / baseline_size) * 100)
        log.warning(
            "  [REGRESSION GUARD] %s shrank from %dB to %dB (>%d%% reduction) -- reverting commit.",
            target_file, baseline_size, current, pct,
        )
    else:
        log.warning(
            "  [REGRESSION GUARD] %s content failed sanity check -- reverting commit.",
            target_file,
        )
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True, timeout=GIT_TIMEOUT, cwd=cwd)


def _normalize_relpath(path):
    """Normalize a repo-relative path for diff comparisons."""
    return Path(path).as_posix()


def _forbidden_changed_files(changed_files, allowed_write_files):
    """Return changed files outside the task's allowed write set."""
    return sorted(f for f in changed_files if f not in allowed_write_files)


def _path_size(path, cwd=None):
    """Return a candidate context file size in bytes, or 0 when unavailable."""
    p = Path(cwd) / path if cwd and not Path(path).is_absolute() else Path(path)
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _build_read_context(spec, test_file, dependencies, specs_by_name, cwd=None):
    """Assemble read-only context files under a configurable budget.

    Always includes the spec file and task test. Dependency target files are
    included in declared order until one of the configured limits is reached.

    Returns ``(read_files, omitted_dependency_targets)``.
    """
    cfg = get_config()
    limits = cfg.get("context_limits", {})
    max_read_files = int(limits.get("max_read_files", 4))
    max_dependency_files = int(limits.get("max_dependency_files", 2))
    max_total_bytes = int(limits.get("max_total_bytes", 24_000))

    read_files = [str(spec["path"]), test_file]
    total_bytes = sum(_path_size(path, cwd=cwd) for path in read_files)

    omitted = []
    included_deps = 0
    for dep_name in dependencies:
        dep_spec = specs_by_name.get(dep_name) if specs_by_name else None
        dep_target = dep_spec.get("target") if dep_spec else None
        if not dep_target:
            continue

        dep_size = _path_size(dep_target, cwd=cwd)
        projected_files = len(read_files) + 1
        projected_bytes = total_bytes + dep_size
        if (
            included_deps >= max_dependency_files
            or projected_files > max_read_files
            or projected_bytes > max_total_bytes
        ):
            omitted.append(dep_target)
            continue

        read_files.append(dep_target)
        total_bytes = projected_bytes
        included_deps += 1

    if omitted:
        log.info(
            "  [context budget] attached %d read file(s), omitted %d dependency target(s): %s",
            len(read_files),
            len(omitted),
            ", ".join(omitted),
        )

    return read_files, omitted


def _final_failure_label(test_output, llm_fail_reasons, had_successful_model_attempt):
    """Choose the terminal task failure label.

    If any model produced a usable edit during the task, the final failing test
    output is the most authoritative signal. LLM-side reasons are only used to
    refine classification when no model ever produced a successful edit.
    """
    test_failure_label = classify_failure(test_output)
    if had_successful_model_attempt:
        return test_failure_label, test_failure_label
    return classify_terminal(test_output, llm_fail_reasons), test_failure_label


def _run_tier_attempts(
    tier_name, start_attempt, message_factory,
    target_file, test_file, read_files, cwd,
    task_name, state, worktree, default_branch,
    start_model, ctx, _elapsed, start_point, base_sha, allowed_write_files,
):
    """Run all attempts for one tier, updating *ctx* in-place.

    Args:
        tier_name:        "primary" or "escalation".
        start_attempt:    First attempt number (1-based; may be > retries when resuming a
                          fully-exhausted tier, in which case the loop body never runs).
        message_factory:  ``(attempt: int) -> str`` — returns the prompt for each attempt.
                          The factory is responsible for running tests and building the
                          "fix the failures" text when needed.
        ctx:              Mutable dict with keys:
                          ``baseline_size``, ``total_attempts``, ``models_tried``,
                          ``task_stats``, ``llm_fail_reasons``.  Updated in-place.

    Returns:
        ``"passed"`` if any attempt passed tests, ``None`` if the tier was exhausted
        without a pass (caller should proceed to the next tier or Stage 3).
    """
    tier = get_tier(tier_name)

    def _accumulate(stats):
        if not stats:
            return
        ctx["task_stats"]["tokens_sent"] += stats.get("tokens_sent", 0)
        ctx["task_stats"]["tokens_received"] += stats.get("tokens_received", 0)
        ctx["task_stats"]["cost_usd"] += stats.get("cost_usd", 0.0)

    for attempt in range(start_attempt, tier["retries"] + 1):
        ctx["total_attempts"] += 1
        log.info("  Attempt %d/%d...", attempt, tier["retries"])

        message = message_factory(attempt)
        head_before = branch_tip("HEAD", cwd=cwd)
        success, model_used, stats, model_attempts = run_with_tier_fallback(
            tier_name, message, target_file, start_model,
            read_files=read_files, cwd=cwd,
        )
        _accumulate(stats)
        for attempt_meta in model_attempts:
            attempted_model = attempt_meta.get("model")
            if attempted_model and attempted_model not in ctx["models_tried"]:
                ctx["models_tried"].append(attempted_model)

            reason = attempt_meta.get("reason")
            if reason in {"invalid_model_config", "forbidden_file_edit"} and reason not in ctx["llm_fail_reasons"]:
                ctx["llm_fail_reasons"].append(reason)
            if attempt_meta.get("success"):
                ctx["had_successful_model_attempt"] = True

        if not success and not model_used:
            tier_models = get_tier(tier_name)["models"]
            if all(is_request_too_large(m) for m in tier_models):
                if "request_too_large" not in ctx["llm_fail_reasons"]:
                    ctx["llm_fail_reasons"].append("request_too_large")
            if all(is_invalid_model(m) for m in tier_models):
                if "invalid_model_config" not in ctx["llm_fail_reasons"]:
                    ctx["llm_fail_reasons"].append("invalid_model_config")

        head_after = branch_tip("HEAD", cwd=cwd)
        head_moved = bool(head_after) and head_after != head_before
        if head_moved:
            changed_files = changed_files_between(head_before, head_after, cwd=cwd)
            forbidden = _forbidden_changed_files(changed_files, allowed_write_files)
            if forbidden:
                if "forbidden_file_edit" not in ctx["llm_fail_reasons"]:
                    ctx["llm_fail_reasons"].append("forbidden_file_edit")
                record_attempt(
                    state, task_name, attempt, tier_name, model_used, success,
                    tests_passed=False, model_attempts=model_attempts,
                )
                save_state(state)
                revert_last_commit(
                    target_file,
                    ctx["baseline_size"],
                    cwd=cwd,
                    reason=(
                        "[FORBIDDEN EDIT] Reverting attempt because it modified files outside the task target: "
                        + ", ".join(forbidden)
                    ),
                )
                continue
        if head_moved and check_regression(target_file, ctx["baseline_size"], cwd=cwd):
            record_attempt(
                state, task_name, attempt, tier_name, model_used, success,
                tests_passed=False, model_attempts=model_attempts,
            )
            save_state(state)
            revert_last_commit(target_file, ctx["baseline_size"], cwd=cwd)
            continue
        if not head_moved and not success:
            # Aider never committed — nothing to revert, but also nothing to test.
            # Move to the next attempt without nuking dependency history.
            log.info("  [no commit produced by aider -- nothing to revert]")
            record_attempt(
                state, task_name, attempt, tier_name, model_used, success,
                tests_passed=False, model_attempts=model_attempts,
            )
            save_state(state)
            continue

        ctx["baseline_size"] = max(ctx["baseline_size"], file_size(target_file, cwd=cwd))
        passed, _ = run_tests(test_file, cwd=cwd)

        record_attempt(
            state, task_name, attempt, tier_name, model_used, success,
            tests_passed=passed, model_attempts=model_attempts,
        )
        save_state(state)

        if passed:
            log.info("PASSED (%s, attempt %d): %s", tier_name, attempt, task_name)
            if not worktree:
                checkout(default_branch)
            record_task(
                state,
                task_name,
                "passed",
                model=model_used,
                attempts=ctx["total_attempts"],
                duration_seconds=_elapsed(),
                models_tried=ctx["models_tried"],
                base_branch=start_point,
                base_sha=base_sha,
                tokens_sent=ctx["task_stats"]["tokens_sent"],
                tokens_received=ctx["task_stats"]["tokens_received"],
                cost_usd=ctx["task_stats"]["cost_usd"],
            )
            save_state(state)
            return "passed"

    return None  # tier exhausted without passing


def _compute_resume_starts(resume_point):
    """Return (skip_primary, primary_start, escalation_start, skip_gemini) from a resume point.

    Given the last recorded attempt, compute which tier and attempt index to
    start from. Returns defaults (False, 1, 1, False) for a fresh run.

    Examples:
      - resume_point=None           -> (False, 1, 1, False)  fresh run
      - last attempt=primary#2      -> (False, 3, 1, False)  continue primary from #3
      - last attempt=escalation#1   -> (True,  1, 2, False)  skip primary, escalation from #2
      - last attempt=gemini#1       -> (True,  1, 1, True)   all tiers done; fall to Stage 3
    """
    if not resume_point:
        return False, 1, 1, False
    if resume_point["tier"] == "gemini":
        # All prior tiers were exhausted in the previous run; skip them all
        # so --resume falls straight to Stage 3 (Claude review).
        return True, 1, 1, True
    if resume_point["tier"] == "escalation":
        return True, 1, resume_point["attempt"] + 1, False
    return False, resume_point["attempt"] + 1, 1, False


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
    task_started_at = datetime.now().astimezone()
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

    # Safety net: always return to the default branch in sequential mode, even
    # if an unexpected exception escapes the attempt loops or setup code.
    # In worktree mode each task has its own isolated working tree so there is
    # no shared HEAD to restore.
    try:
        return _run_task_body(
            spec, default_branch, state, branch_name, task_name,
            target_file, test_file, dependencies, implement_message,
            resume, worktree, branch_preexisted, cwd, models_tried,
            _elapsed, specs_by_name, task_started_at,
        )
    finally:
        if not worktree:
            try:
                checkout(default_branch)
            except Exception:
                pass  # best-effort; don't shadow the original exception


def _run_task_body(
    spec, default_branch, state, branch_name, task_name,
    target_file, test_file, dependencies, implement_message,
    resume, worktree, branch_preexisted, cwd, models_tried,
    _elapsed, specs_by_name, task_started_at,
):
    """Inner implementation of run_task, separated so run_task can wrap it in
    a try/finally that guarantees HEAD is restored to default_branch."""

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
            fail_log = reserve_log_path(f"FAILED-{task_name}")
            write_timestamped_log(
                fail_log,
                f"Previously attempted -- still failing on re-run.\n\n{output}",
                started_at=task_started_at,
            )
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
        start_point, extra_merges = resolve_dependency_base(dependencies, default_branch, cwd=cwd)
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
                fail_log = reserve_log_path(f"FAILED-{task_name}")
                write_timestamped_log(
                    fail_log,
                    (
                        f"Merge conflict while assembling dependencies for {task_name}.\n"
                        f"Conflicting branch: {dep_branch}\n"
                        "Resolve by running the tasks with fewer simultaneous dependencies, "
                        "or fix the conflict manually and re-run the orchestrator.\n"
                    ),
                    started_at=task_started_at,
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
    allowed_write_files = {_normalize_relpath(target_file)}

    # Assemble read-only context under a configurable file/size budget.
    read_files, omitted_dep_targets = _build_read_context(
        spec, test_file, dependencies, specs_by_name, cwd=cwd,
    )
    context_note = ""
    if omitted_dep_targets:
        context_note = (
            "\nRead-only dependency context was trimmed for token budget. "
            "Omitted upstream target files: "
            + ", ".join(omitted_dep_targets)
            + ". Rely on the spec contracts and attached tests for those dependencies."
        )

    primary_tier = get_tier("primary")
    escalation_tier = get_tier("escalation")

    # Mutable context shared across both tier loops.
    ctx = {
        "baseline_size": baseline_size,
        "total_attempts": resume_point["total_attempts"] if resume_point else 0,
        "models_tried": models_tried,
        "task_stats": {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0},
        "llm_fail_reasons": [],
        "had_successful_model_attempt": False,
    }

    # On resume, determine where to start: skip tiers/attempts already done
    skip_primary, primary_start, escalation_start, skip_gemini = _compute_resume_starts(resume_point)

    first_model = primary_tier["models"][0]

    # --- Stage 1: Primary tier ---
    if not skip_primary:
        log.info("Stage 1: Primary tier (%d attempts, starting at %d)", primary_tier["retries"], primary_start)

        def primary_message(attempt):
            if attempt == primary_start and not resume_point:
                return implement_message + context_note
            _, test_output = run_tests(test_file, cwd=cwd)
            return (
                f"Tests failed. Output:\n{_strip_urls(test_output)}\n"
                f"Fix the code to pass all tests.{context_note}"
            )

        result = _run_tier_attempts(
            "primary", primary_start, primary_message,
            target_file, test_file, read_files, cwd,
            task_name, state, worktree, default_branch,
            first_model, ctx, _elapsed, start_point, base_sha, allowed_write_files,
        )
        if result == "passed":
            return "passed"

    # --- Stage 2: Escalation tier ---
    log.info("Stage 2: Escalation (%d attempts, starting at %d)", escalation_tier["retries"], escalation_start)

    def escalation_message(_attempt):
        _, test_output = run_tests(test_file, cwd=cwd)
        return (
            f"Previous model failed. Tests output:\n{_strip_urls(test_output)}\n"
            f"Analyze carefully and fix.{context_note}"
        )

    result = _run_tier_attempts(
        "escalation", escalation_start, escalation_message,
        target_file, test_file, read_files, cwd,
        task_name, state, worktree, default_branch,
        None, ctx, _elapsed, start_point, base_sha, allowed_write_files,
    )
    if result == "passed":
        return "passed"

    # --- Stage 2.5: Gemini tier ---
    # Tried after escalation, before flagging for Claude review.
    # Each model gets retries=1 attempt. Daily quota exhaustion skips that
    # model immediately; if all Gemini models are exhausted, we fall through
    # to Stage 3 and notify the user that Gemini was also unavailable.
    try:
        gemini_tier = get_tier("gemini")
    except ValueError:
        gemini_tier = None

    if gemini_tier and not skip_gemini:
        log.info("Stage 2.5: Gemini tier (%d attempt(s) per model)", gemini_tier["retries"])

        def gemini_message(_attempt):
            _, test_output = run_tests(test_file, cwd=cwd)
            return (
                f"Previous models failed. Tests output:\n{_strip_urls(test_output)}\n"
                f"Analyze carefully and fix.{context_note}"
            )

        result = _run_tier_attempts(
            "gemini", 1, gemini_message,
            target_file, test_file, read_files, cwd,
            task_name, state, worktree, default_branch,
            None, ctx, _elapsed, start_point, base_sha, allowed_write_files,
        )
        if result == "passed":
            return "passed"

        if all_gemini_quota_exhausted():
            ctx["llm_fail_reasons"].append("gemini_quota_exhausted")
            log.warning("  [Gemini tier: all models daily-quota-exhausted]")

    # --- Stage 3: Flag for Claude review ---
    _, test_output = run_tests(test_file, cwd=cwd)
    failure_label, test_failure_label = _final_failure_label(
        test_output,
        ctx["llm_fail_reasons"],
        ctx["had_successful_model_attempt"],
    )
    FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fail_log = reserve_log_path(f"FAILED-{task_name}")
    llm_context = (
        f"LLM failure reason: {', '.join(ctx['llm_fail_reasons'])}\n"
        if ctx["llm_fail_reasons"] else ""
    )
    gemini_note = f"+ gemini ({gemini_tier['retries']}x) " if gemini_tier else ""
    write_timestamped_log(
        fail_log,
        (
            f"Failed after primary ({primary_tier['retries']}x) "
            f"+ escalation ({escalation_tier['retries']}x) "
            f"{gemini_note}all exhausted.\n"
            f"Failure class: {failure_label}\n"
            f"Test failure class: {test_failure_label}\n"
            f"{llm_context}"
            f"Models tried: {', '.join(ctx['models_tried']) or 'none'}\n"
            f"Tokens (sent/received): {ctx['task_stats']['tokens_sent']} / {ctx['task_stats']['tokens_received']}\n"
            f"Cost: ${ctx['task_stats']['cost_usd']:.4f}\n\n"
            f"{test_output}"
        ),
        started_at=task_started_at,
    )
    log.warning("ESCALATE TO CLAUDE: %s (failure_class=%s)", task_name, failure_label)
    if not worktree:
        checkout(default_branch)
    record_task(
        state,
        task_name,
        "failed",
        attempts=ctx["total_attempts"],
        duration_seconds=_elapsed(),
        models_tried=ctx["models_tried"],
        failure_class=failure_label,
        test_failure_class=test_failure_label,
        llm_fail_reasons=ctx["llm_fail_reasons"],
        base_branch=start_point,
        base_sha=base_sha,
        tokens_sent=ctx["task_stats"]["tokens_sent"],
        tokens_received=ctx["task_stats"]["tokens_received"],
        cost_usd=ctx["task_stats"]["cost_usd"],
    )
    save_state(state)
    return "failed"
