# Failure Playbook

This playbook covers two failure modes:

1. **Per-task failure** — `forgeLogs/FAILED-task-NNN-name-<timestamp>.log` exists.
   The primary tier, the escalation tier, **and** the Gemini tier all failed
   (or had their daily quota exhausted) after their full retry budget on a
   single task.
2. **Integration gate failure** — `forgeLogs/INTEGRATION-FAILED-<timestamp>.log` exists.
   All tasks individually passed, but combining them on
   `integration/run-<timestamp>` hit a merge conflict or a full-suite
   regression that Gemini could not fix. Jump to *Integration gate failures* below.

---

## Trigger

The user says: **"Fix the failures"** or **"Fix task NNN"**

---

## Procedure

For each failed task (or the specific task named):

### 1. Understand what was asked
Read `specs/task-NNN-name.md` — focus on the YAML frontmatter (target, test,
dependencies) and the function signatures / constraints.

### 2. Understand what went wrong
Read `forgeLogs/FAILED-task-NNN-name-<timestamp>.log`. The header now contains:
- A **failure class** tag — one of `rate_limit`, `request_too_large`,
  `collection_error`, `missing_symbol`, `assertion`, `timeout`,
  `regression_guard`, `merge_conflict`, or `unknown`.
- The list of models tried in order.
- The full pytest output from the last attempt.

Use the failure class to pick a strategy before reading the body:

| Class | What it usually means | First move |
|---|---|---|
| `rate_limit` | Groq throttled every tier | Wait, re-run — not a code bug |
| `gemini_quota_exhausted` | Gemini daily quota hit before it could fix | Wait until midnight UTC, or fix with Claude now |
| `request_too_large` | Spec exceeds the model's TPM cap | Split the spec into smaller tasks |
| `collection_error` | pytest could not even import the test file | Fix imports or missing module in target file |
| `missing_symbol` | `ImportError` / `AttributeError` / `NameError` | Add the symbol the test expects, or fix the spec signature |
| `assertion` | Tests ran but produced wrong answers | Genuine logic bug — debug normally |
| `timeout` | Test hung or took too long | Infinite loop / deadlock in the implementation |
| `regression_guard` | Our sanity check tripped | Model truncated the file; restart from a clean checkout |
| `merge_conflict` | Dependency branches conflicted | Reshape the dependency graph (stack instead of fan-in) |
| `unknown` | No rule matched | Read the full log |

You can also run `cat pipeline-state.json` and look at the task entry
for its recorded `model`, `models_tried`, `llm_fail_reasons`,
`duration_seconds`, `tokens_sent`, `tokens_received`, `cost_usd`,
`base_branch`, and `base_sha`. `llm_fail_reasons` lists any
automated-tier-level signals (e.g. `gemini_quota_exhausted`) that
explain why the pipeline stopped before reaching Claude. `base_branch`
tells you which branch the task was stacked on — useful when diagnosing
dependency ordering bugs.

### 3. Get on the branch
```bash
git checkout task/task-NNN-name
```

Note: this branch is already stacked on its declared dependencies, so
when you `git log` you will see the dependency task commits as parents.
That is expected and correct — do **not** try to rebase it onto the
default branch.

### 4. Read the current implementation
The model may have produced partial or incorrect code. Read the target file
to understand what state it's in.

### 5. Diagnose the root cause

**Spec problem indicators:**
- Test imports a function that doesn't match the spec signature
- Test expects behavior the spec doesn't describe
- Spec references dependencies that don't exist yet
- Spec is ambiguous — two valid interpretations possible

→ Fix the **spec AND tests first**, then fix the implementation.

**Implementation problem indicators:**
- Functions exist but have wrong logic
- Missing edge case handling
- Wrong library or pattern used
- Model produced a stub or placeholder

→ Fix the **implementation directly**. Don't regenerate from scratch unless
the code is a total dead end.

### 6. Verify the fix
```bash
pytest tests/test_NNN_name.py -v
```

All tests must pass. If any fail, keep iterating.

### 7. Commit
```bash
git add <changed-files>
git commit -m "fix: task-NNN — <what you fixed>"
```

### 8. Stay on the branch
Do NOT return to the default branch. The orchestrator detects passing branches
on re-run and skips them automatically.

---

## After fixing all failures

Tell the user to re-run:
```bash
python3 -m orchestrator
```

Fixed tasks are detected as passing and skipped. Still-failing tasks get
new failure logs. Once every per-task failure is resolved, the
orchestrator automatically runs the **integration gate** — do not
merge to the default branch until that gate reports success.

---

## Integration gate failures

`forgeLogs/INTEGRATION-FAILED-<timestamp>.log` is written when all individual tasks
pass but assembling them together fails. The log distinguishes two
sub-cases; `pipeline-state.json` also records
`integration.status` as `merge_conflict` or `tests_failed`.

### Case A — Merge conflict

The log names the first task branch whose merge conflicted. The
integration branch has already been deleted; the task branches are
untouched.

1. Identify the two tasks that collide. The named task conflicts with
   one of the already-merged tasks — read both specs to confirm which.
2. Decide how to un-collide them:
   - **Linearise:** add one task as a dependency of the other so they
     no longer run in parallel. The later task will then be stacked on
     the earlier task and see its code.
   - **Split:** extract the overlapping surface into a new shared task
     that both tasks depend on.
   - **Narrow scope:** rewrite one spec so it no longer touches the
     overlapping file.
3. `make validate` to confirm the new graph is acyclic, then `make run`.

Do **not** try to fix the merge conflict by hand on an ad-hoc branch —
the orchestrator will throw it away on the next run.

### Case B — Tests failed on the combined branch

The orchestrator automatically attempted a Gemini fix before writing the
failure log. If Gemini succeeded, the gate passed and this log does not
exist. If you are reading this, Gemini either failed or had its daily quota
exhausted (the log will say which).

The full pytest output is in the log and the `integration/run-*`
branch is left on disk so you can reproduce.

1. `git checkout integration/run-<timestamp>`
2. `pytest tests/` — reproduce the failure.
3. Identify which task introduced the regression. `git log --oneline`
   on the integration branch shows the merge commits in order; the
   last task merged before the failing test started failing is a good
   first suspect.
4. `git checkout task/<that-task>` and fix the regression **on the
   task branch**, not on the integration branch.
5. `pytest tests/test_NNN_name.py -v` to confirm the task's own test
   still passes.
6. `make run` — the orchestrator will see the task branch is still
   passing, skip it, rebuild a fresh integration branch, and re-run
   the gate.

The old `integration/run-*` branch can be deleted at any point:

```bash
git branch -D integration/run-<timestamp>
```
