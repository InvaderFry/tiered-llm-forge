"""Git operations for branch management and regression reverting."""

import subprocess
from pathlib import Path

from .runner import file_size


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
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            return ref[len(prefix):]

    # 2. init.defaultBranch — git config fallback
    result = subprocess.run(
        ["git", "config", "--get", "init.defaultBranch"],
        capture_output=True, text=True,
    )
    configured = result.stdout.strip()
    if configured:
        return configured

    # 3. Current branch, only if it isn't one we create ourselves
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    branch = result.stdout.strip()
    if branch and not branch.startswith("task/") and not branch.startswith("integration/"):
        return branch

    # 4. Last-ditch fallback
    return "master"


def ensure_default_branch_exists():
    """Create an initial commit if the repo has no commits yet."""
    result = subprocess.run(["git", "log", "--oneline", "-1"], capture_output=True, text=True)
    if result.returncode != 0:
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "chore: initial commit"],
            check=True
        )


def branch_exists(branch_name):
    """Check if a git branch exists locally."""
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def checkout(branch_name, create=False, start_point=None):
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
    subprocess.run(cmd, check=True)


def current_branch():
    """Return the current branch name, or empty string if detached."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def merge_branch(branch_name, message=None):
    """
    Merge ``branch_name`` into the current branch with --no-ff.

    Returns True on success, False on conflict (and aborts the merge).
    """
    if message is None:
        message = f"merge: {branch_name}"
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-edit", "-m", message, branch_name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Abort any in-progress merge so the working tree stays clean
        subprocess.run(["git", "merge", "--abort"], capture_output=True)
        print(f"  [merge conflict merging {branch_name}: {result.stdout}{result.stderr}]")
        return False
    return True


def delete_branch(branch_name, force=False):
    """Delete a local branch. Use force=True for unmerged branches."""
    flag = "-D" if force else "-d"
    subprocess.run(
        ["git", "branch", flag, branch_name],
        capture_output=True, text=True,
    )


def branch_tip(branch_name):
    """Return the SHA of a branch tip, or empty string if it doesn't exist."""
    result = subprocess.run(
        ["git", "rev-parse", branch_name],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def revert_last_commit(target_file, baseline_size):
    """Undo the last commit and print a warning about the regression."""
    current = file_size(target_file)
    if baseline_size > 0:
        pct = int((1 - current / baseline_size) * 100)
        print(
            f"  [REGRESSION GUARD] {target_file} shrank from {baseline_size}B to {current}B "
            f"(>{pct}% reduction) -- reverting commit."
        )
    else:
        print(
            f"  [REGRESSION GUARD] {target_file} content failed sanity check -- reverting commit."
        )
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True)
