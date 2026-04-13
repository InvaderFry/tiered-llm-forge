"""Parallel task execution using git worktrees and concurrent.futures."""

import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .log import get_logger
from .git_ops import branch_exists, GIT_TIMEOUT, resolve_dependency_base

log = get_logger("parallel")


def find_parallel_groups(ordered_specs):
    """Partition topologically-sorted specs into groups that can run concurrently.

    Two tasks can run in parallel if neither depends on the other. Each task is
    placed in the *earliest* wave where all its dependencies are in prior waves.
    This is a single-pass O(n) algorithm: for each task, its wave index is one
    greater than the maximum wave index of its dependencies (or wave 0 if it has
    none).

    Returns a list of lists, where each inner list is a group of spec dicts
    that can execute concurrently.
    """
    wave_of: dict = {}
    for spec in ordered_specs:
        deps = spec.get("dependencies", []) or []
        w = max((wave_of[d] for d in deps if d in wave_of), default=-1) + 1
        wave_of[spec["task_name"]] = w

    # Group specs by wave index, preserving topo order within each wave.
    max_wave = max(wave_of.values(), default=-1)
    groups: list = [[] for _ in range(max_wave + 1)]
    for spec in ordered_specs:
        groups[wave_of[spec["task_name"]]].append(spec)

    return [g for g in groups if g]


class WorktreePool:
    """Manages git worktrees for parallel task execution."""

    def __init__(self, base_dir=None):
        if base_dir is None:
            repo_name = Path.cwd().resolve().name
            self.base_dir = Path(tempfile.mkdtemp(prefix=f"forge-worktrees-{repo_name}-"))
        else:
            self.base_dir = Path(base_dir).resolve()
            self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(self, branch_name, start_point):
        """Create a worktree for a branch. Returns the worktree path."""
        safe_name = branch_name.replace("/", "_")
        wt_path = self.base_dir / safe_name

        with self._lock:
            if wt_path.exists():
                shutil.rmtree(wt_path)

            cmd = ["git", "worktree", "add", str(wt_path), "-b", branch_name, start_point]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
            if result.returncode != 0:
                # Branch might already exist — try without -b
                cmd = ["git", "worktree", "add", str(wt_path), branch_name]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
                if result.returncode != 0:
                    log.error("Failed to create worktree for %s: %s", branch_name, result.stderr)
                    return None

        return wt_path.resolve()

    def remove(self, branch_name):
        """Remove a worktree."""
        safe_name = branch_name.replace("/", "_")
        wt_path = self.base_dir / safe_name

        with self._lock:
            if wt_path.exists():
                subprocess.run(
                    ["git", "worktree", "remove", str(wt_path), "--force"],
                    capture_output=True, text=True, timeout=GIT_TIMEOUT,
                )

    def cleanup(self):
        """Remove all worktrees and prune."""
        if self.base_dir.exists():
            shutil.rmtree(self.base_dir, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )


def _run_task_in_worktree(spec, default_branch, state, run_task_fn, pool,
                          specs_by_name, resume):
    """Set up a worktree for a task, run it, and clean up.

    The worktree is created on the correct start point (dependency tip or
    default branch). Dependency merges are left to ``run_task`` which
    already handles them with proper ``cwd`` and state recording.

    Returns (task_name, outcome).
    """
    task_name = spec["task_name"]
    branch_name = f"task/{task_name}"
    dependencies = spec.get("dependencies", []) or []

    start_point, _ = resolve_dependency_base(dependencies, default_branch)

    already_exists = branch_exists(branch_name)
    if already_exists:
        wt_path = pool.create(branch_name, branch_name)
    else:
        wt_path = pool.create(branch_name, start_point)

    if wt_path is None:
        log.error("[parallel] Failed to create worktree for %s", task_name)
        return task_name, "failed"

    try:
        outcome = run_task_fn(
            spec, default_branch, state,
            specs_by_name=specs_by_name, resume=resume,
            cwd=str(wt_path), branch_preexisted=already_exists,
        )
        return task_name, outcome
    except Exception as exc:
        log.error("[parallel] Task %s raised: %s", task_name, exc)
        return task_name, "failed"
    finally:
        pool.remove(branch_name)


def run_parallel_group(group, default_branch, state, run_task_fn, specs_by_name=None,
                       resume=False, max_workers=None):
    """Run a group of independent tasks concurrently using git worktrees.

    Each task gets its own worktree so it has an isolated working tree HEAD.
    A ``ThreadPoolExecutor`` runs the tasks in parallel, bounded by
    ``max_workers`` (defaults to ``min(len(group), 4)``).

    Args:
        group: List of spec dicts (tasks with no mutual dependencies).
        default_branch: The default git branch name.
        state: Pipeline state dict (thread-safe via locks in state.py).
        run_task_fn: The run_task callable from task_runner.
        specs_by_name: Map of task_name -> spec dict for dependency context.
        resume: Whether to use --resume mode.
        max_workers: Maximum concurrent tasks. Defaults to min(len(group), 4).

    Returns:
        Dict mapping task_name -> outcome ("passed", "failed", "skipped").
    """
    results = {}

    if len(group) == 1:
        # Single task — no need for worktree overhead, run directly
        spec = group[0]
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

    workers = max_workers or min(len(group), 4)
    log.info(
        "\n[parallel] Wave has %d independent tasks, running with %d workers",
        len(group), workers,
    )

    pool = WorktreePool()
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_task_in_worktree,
                    spec, default_branch, state, run_task_fn, pool,
                    specs_by_name, resume,
                ): spec["task_name"]
                for spec in group
            }

            for future in as_completed(futures):
                task_name = futures[future]
                try:
                    _, outcome = future.result()
                    results[task_name] = outcome
                except Exception as exc:
                    log.error("[parallel] Task %s raised: %s", task_name, exc)
                    results[task_name] = "failed"
    finally:
        pool.cleanup()

    return results
