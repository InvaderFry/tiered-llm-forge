"""Git operations for branch management and regression reverting."""

import subprocess
from pathlib import Path

from .runner import file_size


def get_default_branch():
    """Determine the default branch name (main, master, etc.)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True
    )
    branch = result.stdout.strip()
    if branch and not branch.startswith("task/"):
        return branch

    result2 = subprocess.run(
        ["git", "config", "--get", "init.defaultBranch"],
        capture_output=True, text=True
    )
    return result2.stdout.strip() or "master"


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


def checkout(branch_name, create=False):
    """Checkout a branch, optionally creating it."""
    cmd = ["git", "checkout"]
    if create:
        cmd.append("-b")
    cmd.append(branch_name)
    subprocess.run(cmd, check=True)


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
