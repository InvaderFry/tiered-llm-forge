# CLAUDE.md — Tiered LLM Coding Workflow Instructions

This project uses an automated pipeline (`python3 -m orchestrator`) that feeds
spec files to cheap models (Qwen3 32B → Kimi K2 → Llama 4 Scout → GPT-OSS 120B).
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
- **Keep specs under ~12,000 characters.** Longer specs get auto-compressed.
- **Declare dependencies in frontmatter.** The orchestrator sorts by dependency
  order, not alphabetical order.
- **Never include implementation code.** Signatures and types only.

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
1. Read `specs/task-NNN-name.md` + `specs/FAILED-task-NNN-name.log`
2. `git checkout task/task-NNN-name`
3. Diagnose: bad spec or bad implementation?
4. Fix, run `pytest tests/test_NNN_name.py -v`, commit
5. Stay on branch — orchestrator detects passing branches on re-run

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
| `make dry-run` | Preview pipeline without calling models |
| `make run` | Run the full pipeline |
| `make status` | Show branches, failures, and pipeline state |

---

## Quick Reference

| User says... | You do... |
|---|---|
| "Build X" | Decompose, write specs + tests + SpecsReadMe.md. End with: "Run `make validate` to check specs before running the pipeline." |
| "Review specs" | Read SpecsReadMe.md, flag issues, edit specs |
| "Fix the failures" | Read spec + log, checkout branch, fix, pytest, commit |
| "Review branches" | Diff each branch vs spec, MERGE / FIX / FLAG |
| "Add task for Y" | Write new spec + test, update SpecsReadMe.md |
| "What's the status?" | `git branch --list "task/*"` + `ls specs/FAILED-*.log` |

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
│   ├── __main__.py         ← CLI entry point
│   ├── config.py           ← reads models.yaml
│   ├── spec_parser.py      ← frontmatter + compression + validation
│   ├── model_router.py     ← model selection + rate limit handling
│   ├── runner.py           ← test execution + regression detection
│   ├── git_ops.py          ← branch management
│   └── state.py            ← pipeline-state.json persistence
├── models.yaml             ← model config (single source of truth)
├── .aider.conf.yml         ← Aider defaults
├── .env                    ← API keys (never committed)
├── specs/
│   ├── SpecsReadMe.md      ← human summary (you generate)
│   ├── task-001-name.md    ← spec files (you generate)
│   └── FAILED-task-*.log   ← written by orchestrator on failure
├── tests/
│   ├── conftest.py         ← sys.path setup
│   └── test_001_name.py    ← test files (you generate)
└── src/
    └── ...                 ← implementation (generated by cheap models)
```
