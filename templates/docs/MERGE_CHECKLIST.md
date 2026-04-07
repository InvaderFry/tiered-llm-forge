# Merge Checklist

After the orchestrator reports all tasks passing, run a quality gate before
merging into the default branch.

---

## Trigger

The user says: **"Review the task branches"** or **"Merge the passing branches"**

---

## Procedure

First, resolve the default branch name for the current repo. None of the
commands below assume `main` — some repos use `master`, `trunk`, or something
else entirely, and `git init` picks whatever `init.defaultBranch` is set to.

```bash
DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null \
    | sed 's@^refs/remotes/origin/@@') \
    || DEFAULT=$(git config --get init.defaultBranch 2>/dev/null) \
    || DEFAULT=master
echo "Default branch: $DEFAULT"
```

Then, for each passing task branch, in dependency order:

### 1. Read the spec
```
specs/task-NNN-name.md
```
Focus on: function signatures, constraints, expected behavior.

### 2. Read the diff
```bash
git diff "$DEFAULT..task/task-NNN-name"
```

### 3. Evaluate three criteria

**Correctness** — Does the implementation satisfy every function signature,
type, and constraint in the spec?

**Integration safety** — Does the diff touch anything outside the spec's
target file? If so, is that change safe and intentional?

**Code quality** — Is the code readable and idiomatic? Minor style issues
are fine. Fix anything that creates real maintenance debt.

### 4. Make a decision

#### MERGE — Meets goals, acceptable quality
```bash
git checkout "$DEFAULT"
git merge --squash "task/task-NNN-name"
git commit -m "feat: task-NNN — <one-line description>"
git branch -d "task/task-NNN-name"
```

#### FIX THEN MERGE — Tests pass but a constraint was missed or quality issue exists
1. Stay on the task branch
2. Make the correction
3. Run `pytest tests/test_NNN_name.py -v` to confirm
4. Commit the fix
5. Merge as above

#### FLAG — Implementation fundamentally misses the spec's intent
Do NOT merge. Write `specs/REVIEW-task-NNN-name.md` with:
- What the implementation actually does
- Why it doesn't meet the spec's intent
- What a correct implementation needs to do differently

Leave the branch in place. Tell the user this task needs a new attempt.

---

## After all branches reviewed

Summarize:
- What was merged (and commit hashes)
- What was fixed-then-merged (and what was fixed)
- What was flagged (with paths to REVIEW notes)
