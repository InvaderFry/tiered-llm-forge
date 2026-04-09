"""Parallel task execution using git worktrees and concurrent.futures."""

import shutil
import subprocess
import threading
from pathlib import Path

from .log import get_logger

log = get_logger("parallel")


def find_parallel_groups(ordered_specs):
    """Partition topologically-sorted specs into groups that can run concurrently.

    Two tasks can run in parallel if neither depends on the other. We walk the
    dependency order and group tasks into "waves": each wave contains tasks
    whose dependencies are all in previous waves.

    Returns a list of lists, where each inner list is a group of spec dicts
    that can execute concurrently.
    """
    completed = set()
    groups = []

    for spec in ordered_specs:
        task_name = spec["task_name"]
        deps = set(spec.get("dependencies", []) or [])

        # Can this task go into the current group?
        # Only if all its deps are in already-completed waves.
        if groups and deps <= completed:
            # Check if it can join the latest group (deps were all in
            # prior groups, not the current one being built)
            current_group_names = {s["task_name"] for s in groups[-1]}
            if deps & current_group_names:
                # Dependency is in the current group — needs a new wave
                completed |= current_group_names
                groups.append([spec])
            else:
                groups[-1].append(spec)
        else:
            if groups:
                completed |= {s["task_name"] for s in groups[-1]}
            groups.append([spec])

    return groups


class WorktreePool:
    """Manages git worktrees for parallel task execution."""

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".worktrees")
        self.base_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()

    def create(self, branch_name, start_point):
        """Create a worktree for a branch. Returns the worktree path."""
        safe_name = branch_name.replace("/", "_")
        wt_path = self.base_dir / safe_name

        with self._lock:
            if wt_path.exists():
                shutil.rmtree(wt_path)

            cmd = ["git", "worktree", "add", str(wt_path), "-b", branch_name, start_point]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                # Branch might already exist — try without -b
                cmd = ["git", "worktree", "add", str(wt_path), branch_name]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    log.error("Failed to create worktree for %s: %s", branch_name, result.stderr)
                    return None

        return wt_path

    def remove(self, branch_name):
        """Remove a worktree."""
        safe_name = branch_name.replace("/", "_")
        wt_path = self.base_dir / safe_name

        with self._lock:
            if wt_path.exists():
                subprocess.run(
                    ["git", "worktree", "remove", str(wt_path), "--force"],
                    capture_output=True, text=True,
                )

    def cleanup(self):
        """Remove all worktrees and prune."""
        if self.base_dir.exists():
            shutil.rmtree(self.base_dir, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True, text=True,
        )


def run_parallel_group(group, default_branch, state, run_task_fn, specs_by_name=None,
                       resume=False):
    """Run a group of independent tasks, serialized within each wave.

    True concurrent execution requires git worktree isolation so that each
    task gets its own working tree HEAD.  Until worktree support is wired
    into ``run_task`` (accepting a ``cwd`` parameter), tasks within a wave
    are executed sequentially.  The wave grouping still provides value: it
    determines the *minimum* number of serial rounds needed, and the
    ``WorktreePool`` infrastructure is ready for a future PR to enable
    actual concurrency.

    Args:
        group: List of spec dicts (tasks with no mutual dependencies).
        default_branch: The default git branch name.
        state: Pipeline state dict (thread-safe via locks in state.py).
        run_task_fn: The run_task callable from task_runner.
        specs_by_name: Map of task_name -> spec dict for dependency context.
        resume: Whether to use --resume mode.

    Returns:
        Dict mapping task_name -> outcome ("passed", "failed", "skipped").
    """
    results = {}

    if len(group) > 1:
        log.info(
            "\n[parallel] Wave has %d independent tasks (running sequentially -- "
            "concurrent execution requires git worktree support, not yet wired in)",
            len(group),
        )

    for spec in group:
        task_name = spec["task_name"]
        try:
            outcome = run_task_fn(
                spec, default_branch, state,
                specs_by_name=specs_by_name, resume=resume,
            )
            results[task_name] = outcome
        except Exception as exc:
            log.error("[parallel] Task %s raised: %s", task_name, exc)
            results[task_name] = "failed"

    return results
