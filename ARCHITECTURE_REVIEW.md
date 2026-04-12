
# Note this Review was done on 04/12/2026 and the changes have been made. Keeping for a bit before fully removing next month — tiered-llm-forge


# Architecture Review — tiered-llm-forge

I've read the whole orchestrator package, the CLI, the parallel/worktree code, the integration gate, state, runner, router, git_ops, and the docs. The architecture is **fundamentally sound** — you do not need to redesign it. Flow is coherent: plan → validate → run (sequential or waves) → integration gate → review/merge. Module boundaries are clean and the new parallel/worktree + resume + observability layers slot in without breaking the core shape.

Below are the issues worth actually fixing, ranked by impact.

---

## Architecture: verdict

The shape is good:

- **spec_parser → topo_sort → task_runner → integration_gate** is a clear pipeline.
- Cross-cutting concerns (state, logging, rate-limit coordinator, failure classifier) live in their own modules and are reused correctly by both sequential and parallel code paths.
- Threading story is principled: `_state_lock` guards `pipeline-state.json`, `_rate_limit_lock` guards coordinator state, and `_request_too_large` is intentionally per-thread via `threading.local` so parallel tasks don't poison each other.
- Worktree mode is layered *on top of* `run_task` via `cwd=` rather than forking a parallel control path — that's the right call.

**Verdict: no redesign needed.** The items below are targeted refactors and bug fixes.

---

## Recommendations (ranked)

### 1. `task_runner.run_task` is 350 lines with duplicated Stage 1 / Stage 2 loops — ⭐⭐⭐⭐⭐ `[Done]`
`templates/orchestrator/task_runner.py:242-379` runs essentially the same loop twice: rate-limit tier → head-move check → regression guard → tests → record_attempt → maybe success. The only real difference is the prompt and the `start_model`. Extract a `_run_tier_attempts(tier_name, start_attempt, ..., first_message_builder)` helper. Drops ~100 LOC, makes resume-math + escalation easier to reason about, and gives you one place to add things like "also record aider stderr" later.

### 2. No tests for `parallel.py` or worktree mode — ⭐⭐⭐⭐⭐ `[Done]`
`tests/` has `test_failure_class`, `test_git_ops`, `test_model_router`, `test_runner`, `test_spec_parser`, `test_state`. The README claims `test_parallel.py` and `test_cwd.py` exist — they don't. `parallel.py` is ~230 LOC of concurrent logic around worktrees, and it is the riskiest module in the repo. At minimum: unit-test `find_parallel_groups` against a handful of dep graphs, and add a test that confirms `run_task(cwd=...)` uses the cwd for git + pytest. Easy wins that protect the hardest code.

### 3. Exception safety: sequential mode can strand HEAD on a task branch — ⭐⭐⭐⭐ `[Done]`
`run_task` does `if not worktree: checkout(default_branch)` in several happy-path/failure-path locations (`task_runner.py:126`, `189`, `302`, `362`, `401`). None are wrapped in `try/finally`. An unexpected exception inside the attempt loop leaves HEAD on the task branch, which confuses the next run because `current_branch()` is no longer the default. Wrap the main body in `try/finally: if not worktree: checkout(default_branch)`.

### 4. `find_parallel_groups` assigns tasks to the *latest* possible wave instead of the *earliest* — ⭐⭐⭐⭐ `[Done]`
`parallel.py:28-49`. Trace: topo order `A (no deps), B (deps A), C (no deps)` → produces `[[A],[B,C]]` instead of `[[A,C],[B]]`. C should run in wave 1 alongside A but ends up stranded with B. Real performance regression for fan-out shapes. The fix is one-pass: for each task, place it in the minimum wave index where all deps are in earlier waves.

```python
wave_of = {}
for spec in ordered_specs:
    deps = spec.get("dependencies") or []
    w = max((wave_of[d] for d in deps if d in wave_of), default=-1) + 1
    wave_of[spec["task_name"]] = w
```

Then group by wave index. Simpler and correct.

### 5. `_resolve_dependency_base` is duplicated in `task_runner.py` and `parallel.py` — ⭐⭐⭐⭐ `[Done]`
Identical logic, two copies (`task_runner.py:37-62` and `parallel.py:103-121`). Move it to `git_ops.py` (where it logically lives — it's dependency→branch resolution) and import from both. Prevents drift on the next bug fix.

### 6. Regression guard's content-marker check can false-positive on legitimate files — ⭐⭐⭐ `[Done]`
`runner.py:146-167`. A `src/constants.py` containing only `FOO = 1` has none of `def `, `class `, `import `, `from ` → `_content_looks_valid` returns False → commit reverts. Same hazard for a small `__init__.py` or a pure-data module. Two options: (a) also accept `=` with an identifier on the LHS for Python, (b) only run content-check when size shrank by >50% vs baseline. Option (b) is safer because it makes the sanity check a *confirmation* of size regression instead of an independent tripwire.

### 7. Resume-point bookkeeping is hairy and untested — ⭐⭐⭐ `[Done]`
`task_runner.py:220-233` computes `skip_primary`, `primary_start`, `escalation_start` from `resume_point`. This logic is worth its own small function plus a unit test: "last attempt was primary#3 (exhausted) → skip_primary=True, escalation_start=1". Today the logic works but it's subtle and regressions would be silent.

### 8. `__main__.py` loads every spec twice — ⭐⭐⭐ `[Done]`
`__main__.py:160-164`:
```python
for sf in ordered:
    _spec = load_spec(sf)
    specs_by_name[_spec["task_name"]] = _spec
ordered_specs = [load_spec(sf) for sf in ordered]
```
First loop builds the map, second comprehension re-reads every file. Collapse to one pass: `ordered_specs = [load_spec(sf) for sf in ordered]; specs_by_name = {s["task_name"]: s for s in ordered_specs}`. Pure I/O waste, but also a trap for subtle mutation bugs later.

### 9. `select_model_for_spec` is a no-op that still accepts `spec_text` — ⭐⭐ `[Done]`
`model_router.py:114-128`. Docstring admits the previous "large context" branch was unreachable and the function now just returns `primary["models"][0]`. Either inline it at the two call sites (`__main__.py` dry-run, `task_runner.py:243`) and delete the function, or commit to it as a future router seam and leave it. Don't leave dead signature surface area indefinitely.

### 10. README + docs drift — ⭐⭐ `[Done]`
`README.md:154-155` and the "Repo structure" section reference `test_parallel.py` and `test_cwd.py` which don't exist. Either add the tests (see #2) or remove the references. Matters because it's the first thing a reviewer checks.

### 11. `git_ops.py` imports `file_size` from `runner.py` just for the regression-guard helper — ⭐⭐ `[Done]`
`git_ops.py:6` and `revert_last_commit` at `git_ops.py:149-163` exist only because `file_size` and the regression percentage live in `runner.py`. Keeps a circular-ish dependency between "git" and "test execution". Move `revert_last_commit` into `runner.py` (or, better, into `task_runner.py` since that's its only caller) and let `git_ops.py` be pure git.

### 12. `pipeline-state.json` loses history across runs — ⭐⭐ `[Done]`
`state.py:32-37`. Every run writes a fresh `run_id` at load time when the file doesn't exist, but otherwise re-uses whatever's on disk. You can't answer "how many runs has task-003 failed this week?" from state alone — you'd need the per-run log files. Not urgent; becomes worth it if you start caring about long-term flake rate. Easy version: append a lightweight run-summary entry to a `runs: []` list on `save_state`.

### 13. `AIDER_TIMEOUT = 300` hardcoded — ⭐ `[Done]`
`model_router.py:18`. Fine today, but a larger escalation model on a long spec can plausibly take >5 min. Worth moving to `models.yaml` (`aider_timeout_seconds`) alongside `cooldown_seconds` so tuning doesn't need a code edit.

---

## What's genuinely good and should not be touched

- **Spec-as-read-only-context** (`task_runner.py:210-216`). Attaching the spec + test + dep targets to aider as `--read` instead of embedding in the prompt is the right call and cleanly sidestepped the old "compress_spec" mess.
- **Per-model rate-limit coordinator with per-thread `request_too_large`.** That's exactly the right granularity — session for time-windowed 429s, task-scoped for TPM-cap exceeded — and the comment explaining why is load-bearing.
- **Integration gate's two-failure-modes split** (merge conflict deletes the branch; test failure keeps it for repro). That's the right UX.
- **Resume mode storing per-attempt history in `state.py`.** Cleaner than trying to re-derive from branch history.
- **Worktree mode wrapping `run_task` via `cwd=`** rather than forking the task-runner. Keeps one source of truth for escalation logic.

---

## TL;DR

Ship-ready. Priority order if you touch it again: **(2) parallel tests**, **(1) extract Stage 1/2 helper**, **(3) try/finally around checkout**, **(4) earliest-wave grouping**, **(5) dedupe `_resolve_dependency_base`**. Everything else is polish.
