# CLAUDE.md — Tiered LLM Coding Workflow Instructions

This project uses an automated pipeline (`python3 -m orchestrator`) that feeds
spec files through escalating model tiers:
**Qwen3 32B → Kimi K2 → Llama 4 Scout → GPT-OSS 120B → Gemini 2.5 Flash**

Your job during planning is to produce **files on disk** that the pipeline can
execute without further human input.

---

## Your Role

You are the **planner and reviewer**. You do not write implementation code
(unless a task has failed automated attempts and you are fixing it). Your outputs:

1. Spec files in `specs/task-NNN-name.md` (with YAML frontmatter)
2. Test files in `tests/test_NNN_name.py`
3. `specs/SpecsReadMe.md` — human-readable summary of all specs
4. Architectural notes or ADRs when asked

The orchestrator picks up every `specs/task-*.md` automatically.

---

## Spec File Format (REQUIRED — parser depends on this)

Every spec **must** have YAML frontmatter. The orchestrator parses this to
determine the target file, test file, and dependency ordering.

```markdown
---
task: "001"
name: "user-models"
target: src/models/user.py
test: tests/test_001_user_models.py
dependencies: []
---

# Task 001: User Models

## Functions to implement
- `create_user(name: str, email: str) -> User`

## Data structures / types
...

## Constraints
- Use dataclasses, not Pydantic

## Context
Brief explanation of why this task exists. This section gets stripped by the
orchestrator to save tokens — keep important details in other sections.

```

**Full spec format reference:** `docs/SPEC_FORMAT.md`

### Critical rules

- **One target file per task.** Split multi-file work into separate tasks.
- **Zero-padded three-digit task numbers:** `task-001`, `task-002`, etc.
- **Keep specs under ~12,000 characters.** Longer specs trigger warnings and should be split before orchestration.
- **Commit planner inputs before orchestration.** Before `make run` or `make parallel`,
  all new or edited task specs and their referenced test files must be tracked by git
  and committed.
- **Declare dependencies in frontmatter.** The orchestrator both sorts *and*
  stacks by dependency order: task N's git branch is created from the tip
  of its dependency branch(es), so the implementer actually sees upstream
  code. Missing a dependency means the implementer runs without it.
- **Keep dependency graphs narrow.** A task with many simultaneous
  dependencies is assembled by merging each dep branch together — if two
  deps touch the same file, you will get a merge conflict and the task
  will fail. Prefer a linear chain (A → B → C) over a fan-in (A, B, C → D)
  whenever the tasks might touch overlapping code.
- **Never include implementation code.** Signatures and types only.

### Quality bar for generated services

- **Prefer production-leaning contracts over toy scaffolding.** If a task creates a service/client/CLI, the spec should demand explicit error handling, timeouts, and stable interfaces.
- **For Java/Spring tasks:** prefer typed DTOs/records over raw `Map` parsing, constructor injection over field injection, explicit HTTP timeouts, and clear non-zero exit behavior for CLI failures.
- **For external calls:** require local mocks in tests and explicit configuration points (`base-url`, output paths, timeouts) so acceptance tests can run without live services.
- **For cross-cutting setup:** if the tests depend on warmed caches or toolchain state (for example `mvn -o`), note that in `SpecsReadMe.md` and keep the narrowest possible acceptance test that still proves the behavior.

---

## SpecsReadMe.md (REQUIRED)

Every planning session must produce `specs/SpecsReadMe.md`. This is the document
the human reads during Phase 2 (manual review). See `docs/SPEC_FORMAT.md` for
the full template.

Key sections: What we're building, Architecture decisions, Task summary table,
Pre-flight checklist, Context that was stripped from specs.

---

## Test File Requirements

Tests define "done." The orchestrator runs `pytest <test_file> -x` after every
Aider attempt. A task passes only when all tests pass and at least one was collected.

- **Name must match** (with underscores): `task-001-user-auth` → `test_001_user_auth.py`
- **Self-contained.** Mock external calls. No network, no DB.
- **Cover acceptance criteria completely.** Not just happy path.
- **Use pytest fixtures**, not `unittest.TestCase`.

---

## Task Decomposition

**Good tasks:** one file, under 500 words, clear pass/fail criteria, no circular deps.

Typical pattern:
```
task-001  Data models (no dependencies)
task-002  Core logic (depends on task-001)
task-003  Storage layer (depends on task-001)
task-004  API layer (depends on task-002 + task-003)
```

---

## Reviewing Failed Tasks

**Trigger:** User says "Fix the failures" or "Fix task NNN"

**Playbook:** See `docs/FAILURE_PLAYBOOK.md` for the full procedure.

Quick version:
1. Read `specs/task-NNN-name.md` + `forgeLogs/FAILED-task-NNN-name-<timestamp>.log`.
   The log header includes a **failure class**
   (`dependency_cache_missing`, `invalid_model_config`, `rate_limit`,
   `gemini_quota_exhausted`, `request_too_large`,
   `collection_error`, `missing_symbol`, `assertion`, `timeout`,
   `forbidden_file_edit`, `regression_guard`, `merge_conflict`, `unknown`) and the list of
   models tried — use these to pick the right fix before reading the
   full output. If you see `gemini_quota_exhausted`, the Gemini tier
   was already attempted but had no daily quota remaining; this is not
   a code bug — the implementation just needs a human fix.
2. `git checkout task/task-NNN-name` (the branch is already stacked
   on its dependencies, so upstream code is present).
3. Diagnose: bad spec or bad implementation?
4. Fix, run `pytest tests/test_NNN_name.py -v`, commit.
5. Stay on branch — orchestrator detects passing branches on re-run.

## Reviewing Integration Gate Failures

**Trigger:** `forgeLogs/INTEGRATION-FAILED-<timestamp>.log` exists, or
`pipeline-state.json` shows `integration.status != "passed"`.

Each task's own test file passes in isolation but combining them on
the integration branch revealed a cross-task problem. Two classes:

- **Merge conflict:** the log names the offending task branch. Fix by
  reshaping the dependency graph (stack the conflicting tasks instead
  of fanning them into a merge) or by editing the conflicting task's
  spec so it does not collide.
- **Test failure:** the orchestrator automatically tried Gemini to fix
  the failure before writing the log. If you see this log, Gemini also
  failed or had its quota exhausted. The `integration/run-*` branch is
  kept on disk. Check it out, reproduce with `pytest tests/`, then fix
  the regression on the **task branch** (not the integration branch).
  The orchestrator rebuilds a fresh integration branch on the next run.

---

## Pre-Merge Branch Review

**Trigger:** User says "Review the task branches" or "Merge the branches"

**Checklist:** See `docs/MERGE_CHECKLIST.md` for full criteria.

Quick version: For each branch, diff against spec, evaluate correctness +
integration safety + code quality, then MERGE / FIX THEN MERGE / FLAG.

---

## Pipeline Commands

| Command | What it does |
|---------|-------------|
| `make validate` | Check all specs for errors before running |
| `make preflight` | Validate config, provider env, and runtime prerequisites |
| `make dry-run` | Preview pipeline without calling models |
| `make run` | Run the full pipeline |
| `make resume` | Resume failed tasks from last attempt instead of flagging for review |
| `make parallel` | Run independent tasks concurrently in dependency waves (default 4 workers) |
| `make status` | Show branches, failures, and pipeline state |

### CLI flags (can also be passed directly)

| Flag | Effect |
|------|--------|
| `--resume` | Resume from the exact attempt that crashed/failed |
| `--parallel [N]` | Run tasks concurrently in dependency waves using git worktrees (N = max workers, default 4) |
| `--verbose` | Enable debug-level output with timestamps |

All runs write debug-level output to `forgeLogs/orchestrator-<timestamp>.log` for post-mortem analysis. Each run gets its own timestamped file so reruns never overwrite previous logs.
The generated `Makefile` also writes command transcripts like
`forgeLogs/run-<timestamp>.log`, `forgeLogs/validate-<timestamp>.log`, and
`forgeLogs/preflight-<timestamp>.log`; all on-disk logs include `Start time:`
at the top and `End time:` at the bottom.

### When to use `--parallel`

`--parallel` partitions the dependency graph into waves and runs each
wave's tasks simultaneously in isolated git worktrees via a thread pool.

**Pros:**
- Faster wall-clock time — independent tasks run concurrently
- Better rate-limit utilisation — idle threads yield to active ones
- Automatic wave grouping from the dependency graph

**Cons:**
- Higher disk usage — each concurrent task gets a full worktree copy
- Interleaved log output — harder to read; use `--verbose` and grep by task name
- Rate-limit amplification — more concurrent requests can trigger more 429s
- Not the default — sequential (`make run`) is simpler to debug

**Rule of thumb:** use `make run` for first runs and debugging. Use
`make parallel` once the specs are validated and you want throughput.
Linear dependency chains see no benefit since each wave has only one task.

---

## Quick Reference

| User says... | You do... |
|---|---|
| "Build X" | Decompose, write specs + tests + SpecsReadMe.md. End with: "Run `make validate`, then `make preflight`, then commit `specs/` and `tests/`, then run `make dry-run` before `make run` or `make parallel`." |
| "Review specs" | Read SpecsReadMe.md, flag issues, edit specs |
| "Fix the failures" | Read spec + log, checkout branch, fix, pytest, commit |
| "Review branches" | Diff each branch vs spec, MERGE / FIX / FLAG |
| "Add task for Y" | Write new spec + test, update SpecsReadMe.md |
| "What's the status?" | `git branch --list "task/*" "integration/*"` + `ls forgeLogs/FAILED-*.log forgeLogs/INTEGRATION-FAILED-*.log 2>/dev/null` |

---

## Project Structure

```
project-root/
├── CLAUDE.md               ← this file
├── ORCHESTRATION.md        ← full workflow walkthrough
├── docs/                   ← reference guides
│   ├── SPEC_FORMAT.md
│   ├── FAILURE_PLAYBOOK.md
│   └── MERGE_CHECKLIST.md
├── orchestrator/           ← pipeline package
│   ├── __main__.py         ← CLI entry point (arg parsing, dispatch)
│   ├── config.py           ← reads models.yaml
│   ├── spec_parser.py      ← frontmatter + validation + topo sort
│   ├── model_router.py     ← model selection + rate limit handling
│   ├── runner.py           ← per-task + full-suite test execution
│   ├── git_ops.py          ← pure-git: branch management, stacking, merging
│   ├── task_runner.py      ← per-task orchestration, model escalation, regression revert
│   ├── integration.py      ← integration gate (merge + full suite)
│   ├── summary.py          ← pipeline run summary + observability
│   ├── parallel.py         ← concurrent task execution via worktrees + thread pool
│   ├── log.py              ← logging config (console + forgeLogs/orchestrator-<timestamp>.log)
│   ├── failure_class.py    ← classifies pytest/aider output
│   └── state.py            ← pipeline-state.json persistence
├── models.yaml             ← model config (tiers, timeouts, cooldown — single source of truth)
├── .aider.conf.yml         ← Aider defaults
├── .env                    ← API keys (never committed)
├── specs/
│   ├── SpecsReadMe.md      ← human summary (you generate)
│   └── task-001-name.md    ← spec files (you generate)
├── forgeLogs/
│   ├── orchestrator-<timestamp>.log  ← full debug log per run
│   ├── run-<timestamp>.log           ← make-run shell transcript
│   ├── validate-<timestamp>.log      ← make-validate shell transcript
│   ├── FAILED-task-*-<timestamp>.log ← written by orchestrator on failure
│   └── INTEGRATION-FAILED-<timestamp>.log ← written on integration failure
├── tests/
│   ├── conftest.py         ← sys.path setup
│   └── test_001_name.py    ← test files (you generate)
└── src/
    └── ...                 ← implementation (generated by cheap models)
```
