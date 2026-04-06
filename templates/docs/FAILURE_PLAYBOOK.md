# Failure Playbook

When a task lands in `specs/FAILED-task-NNN-name.log`, it means the primary
model tier **and** the escalation tier both failed after their full retry budget.

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
Read `specs/FAILED-task-NNN-name.log`. This contains:
- Which models were attempted and how many times
- The full pytest output from the last attempt
- Whether the failure was a rate limit, regression, or logic error

### 3. Get on the branch
```bash
git checkout task/task-NNN-name
```

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
new failure logs.
