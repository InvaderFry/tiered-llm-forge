"""Git operations for branch management."""

import subprocess
from pathlib import Path

from .log import get_logger

log = get_logger("git_ops")

# Timeout for git commands (seconds)
GIT_TIMEOUT = 30


def get_default_branch():
    """Determine the default branch name (main, master, etc.).

    Resolution order, from most to least authoritative:

    1. ``refs/remotes/origin/HEAD`` symbolic ref — set by ``git clone``
       and the only signal that survives a developer being checked out
       on a feature branch.
    2. ``init.defaultBranch`` git config — what ``git init`` would have
       picked. Always set in modern git installs.
    3. The current branch, but only if it isn't a working branch
       (``task/*`` or ``integration/*``). Older guards only excluded
       ``task/*``, which would silently treat an integration branch as
       the default if the orchestrator was launched from one.
    4. Fallback to ``master`` so callers always get a non-empty string.
    """
    # 1. origin/HEAD — works as soon as the repo has a remote
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            return ref[len(prefix):]

    # 2. init.defaultBranch — git config fallback
    result = subprocess.run(
        ["git", "config", "--get", "init.defaultBranch"],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    configured = result.stdout.strip()
    if configured:
        return configured

    # 3. Current branch, only if it isn't one we create ourselves
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    branch = result.stdout.strip()
    if branch and not branch.startswith("task/") and not branch.startswith("integration/"):
        return branch

    # 4. Last-ditch fallback
    return "master"


def ensure_default_branch_exists(cwd=None):
    """Create an initial commit if the repo has no commits yet."""
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "chore: initial commit"],
            check=True, timeout=GIT_TIMEOUT, cwd=cwd,
        )


def branch_exists(branch_name, cwd=None):
    """Check if a git branch exists locally."""
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
    )
    return bool(result.stdout.strip())


def checkout(branch_name, create=False, start_point=None, cwd=None):
    """
    Checkout a branch, optionally creating it from a given start point.

    If ``start_point`` is provided and ``create`` is True, the new branch
    is created from that ref instead of from the current HEAD.
    """
    cmd = ["git", "checkout"]
    if create:
        cmd.append("-b")
    cmd.append(branch_name)
    if create and start_point:
        cmd.append(start_point)
    subprocess.run(cmd, check=True, timeout=GIT_TIMEOUT, cwd=cwd)


def current_branch(cwd=None):
    """Return the current branch name, or empty string if detached."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
    )
    return result.stdout.strip()


def merge_branch(branch_name, message=None, cwd=None):
    """
    Merge ``branch_name`` into the current branch with --no-ff.

    Returns True on success, False on conflict (and aborts the merge).
    """
    if message is None:
        message = f"merge: {branch_name}"
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-edit", "-m", message, branch_name],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
    )
    if result.returncode != 0:
        # Abort any in-progress merge so the working tree stays clean
        subprocess.run(["git", "merge", "--abort"], capture_output=True, timeout=GIT_TIMEOUT, cwd=cwd)
        log.warning("  [merge conflict merging %s: %s%s]", branch_name, result.stdout, result.stderr)
        return False
    return True


def delete_branch(branch_name, force=False, cwd=None):
    """Delete a local branch. Use force=True for unmerged branches."""
    flag = "-D" if force else "-d"
    subprocess.run(
        ["git", "branch", flag, branch_name],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
    )


def branch_tip(branch_name, cwd=None):
    """Return the SHA of a branch tip, or empty string if it doesn't exist."""
    result = subprocess.run(
        ["git", "rev-parse", branch_name],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_dependency_base(dependencies, default_branch, cwd=None):
    """Determine the start point for a new task branch based on its dependencies.

    - 0 deps   -> default branch
    - 1 dep    -> that dep's task branch (stacked)
    - N deps   -> default branch (caller will merge deps in afterwards)

    Returns ``(start_point, extra_merges)`` where ``extra_merges`` is the list
    of dependency branches the caller still needs to merge in after checkout.
    Any dependency whose branch does not exist is skipped with a warning.
    ``cwd`` is forwarded to ``branch_exists`` (needed for worktree callers).
    """
    dep_branches = []
    for dep in dependencies:
        dep_branch = f"task/{dep}"
        if branch_exists(dep_branch, cwd=cwd):
            dep_branches.append(dep_branch)
        else:
            log.warning("  [dependency '%s' has no branch -- falling back to default for that dep]", dep)

    if not dep_branches:
        return default_branch, []
    if len(dep_branches) == 1:
        return dep_branches[0], []
    # Multiple deps: start from default and merge each one in
    return default_branch, dep_branches


def changed_files_between(old_ref, new_ref, cwd=None):
    """Return repo-relative files changed between two refs."""
    result = subprocess.run(
        ["git", "diff", "--name-only", old_ref, new_ref],
        capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def validate_tracked_clean(paths, cwd=None):
    """Return validation errors for spec/test inputs that must be tracked and clean.

    Worktree mode only sees files that are committed into the main repository.
    If specs/tests are untracked or dirty, parallel runs can execute against a
    different input set than the user expects and merges can fail on untracked
    overwrite errors. This preflight makes that state explicit before any model
    work starts.
    """
    errors = []
    seen = set()
    for path in paths:
        rel = str(path)
        if not rel or rel in seen:
            continue
        seen.add(rel)

        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
        )
        if tracked.returncode != 0:
            errors.append(
                f"{rel} is not tracked by git. Commit specs/tests before running the orchestrator."
            )
            continue

        status = subprocess.run(
            ["git", "status", "--porcelain", "--", rel],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
        )
        if status.stdout.strip():
            errors.append(
                f"{rel} has uncommitted changes. Commit or stash specs/tests before running the orchestrator."
            )

    return errors

