# CLAUDE.md — Tiered LLM Coding Workflow Instructions

This project uses an automated pipeline (`orchestrator.py`) that feeds spec files
to cheap models (Qwen3 32B → Kimi K2 → Llama 4 Scout → GPT-OSS 120B). Your job
during planning is to produce **files on disk** that the pipeline can execute
without further human input. Read these rules carefully before generating any specs.

---

## Your Role in This Workflow

You are the **planner and reviewer**. You do not write implementation code directly
(unless a task has failed automated attempts and you are fixing it). Your outputs are:

1. Spec files in `specs/task-NNN-name.md`
2. Test files in `tests/test-NNN-name.py`
3. `specs/SpecsReadMe.md` — a human-readable summary of all specs written this session
4. Architectural notes or ADRs when the user asks

The orchestrator picks up every `specs/task-*.md` automatically. Do not create
specs outside that naming pattern. `SpecsReadMe.md` is ignored by the orchestrator
but is critical for human review before the pipeline runs.

---

## SpecsReadMe.md — Human Review Document (REQUIRED)

Every planning session must produce a `specs/SpecsReadMe.md` file alongside the
spec and test files. This is the document the human reads during Phase 2 (manual
review) instead of opening each spec individually.

**Why this exists:** Spec files are written for cheap models — function signatures,
types, constraints, no prose. The `## Context` section gets stripped by the
orchestrator to save tokens. `SpecsReadMe.md` is where that context lives for the
human reviewer. It replaces skimming through a dozen terse markdown files with one
plain-language walkthrough of what's about to be built and why.

### What SpecsReadMe.md must contain

```markdown
# Specs — [Feature Name] — [Date]

## What we're building
One paragraph plain-English summary of the feature. What problem does it solve?
What will it do when complete?

## Architecture decisions
Bullet list of any non-obvious design choices made during decomposition:
why a particular library was chosen, why something was split into multiple tasks
rather than one, what tradeoffs were made.

## Task summary

| Task | File | What it does | Depends on |
|------|------|-------------|------------|
| task-001-models | src/models/user.py | Defines User and AuthResult dataclasses | — |
| task-002-auth | src/auth/login.py | authenticate_user, token creation/validation | task-001 |
| task-003-api | src/api/endpoints.py | POST /login and /logout routes | task-002 |

## Things to check before running the pipeline
- [ ] Tasks are numbered in dependency order (dependencies have lower numbers)
- [ ] Each task touches only one file
- [ ] Tests are self-contained (no network, no unfinished dependencies)
- [ ] No spec references a file that doesn't exist yet (check the Dependencies sections)
- [ ] Estimated total: N tasks, roughly N–N hours of cheap model time

## Context that was stripped from specs
Brief notes on anything that was intentionally left out of the machine-readable specs
to save tokens, but that a human reviewer should know. E.g. "bcrypt was chosen over
argon2 because the existing codebase uses it in src/legacy/auth.py — see that file
for the current pattern."
```

### Rules for SpecsReadMe.md

- **Write it last**, after all specs and tests for the session are complete.
  It should reflect the final state of what was written, not an early plan.
- **Update it incrementally** when adding tasks mid-session. Don't rewrite from
  scratch — append new rows to the task table and update the checklist.
- **Keep the task table in sync** with the actual spec files. If you rename or
  renumber a spec, update the table immediately.
- **The "Context that was stripped" section is mandatory** whenever a spec had
  a `## Context` section that will be auto-compressed. This is the whole point —
  the human needs that context somewhere even if the model doesn't.
- **Plain English only.** No code blocks in the summary sections. Code belongs
  in specs. This document is for a human deciding whether to hit Enter on the
  pipeline, not for a model.

---

## Spec File Format (REQUIRED — parser depends on this)

Every spec file **must** follow this exact structure. The orchestrator's
`parse_target_file()` function looks for the `## Target file` section header
followed immediately by the path on the next line.

```markdown
# Task NNN: Short Descriptive Name

## Target file
src/module/filename.py

## Dependencies
- src/other/module.py (read-only reference — do not modify)
- src/config/settings.py (read-only reference — do not modify)

## Functions to implement
- `function_name(param: type, param2: type) -> ReturnType`
- `another_function(param: type) -> ReturnType`

## Data structures / types
```python
from dataclasses import dataclass

@dataclass
class ExampleResult:
    success: bool
    value: str
    error: str | None = None
```

## Constraints
- Specific library to use (e.g., "use bcrypt for password hashing")
- Patterns to follow (e.g., "follow the pattern in src/existing/module.py")
- Performance or correctness requirements

## Context
One short paragraph explaining WHY this task exists and how it fits into the
larger feature. Include any non-obvious domain knowledge the implementing model
will need. Keep this concise — cheap models have limited context windows.

## Test file
tests/test-NNN-name.py (already written — implementation must pass all tests)
```

### Critical rules for specs

- **One target file per task.** If a feature genuinely needs multiple files,
  split it into multiple tasks with explicit dependency ordering.
- **Number tasks with zero-padded three digits:** `task-001`, `task-002`, etc.
  Tasks run in alphabetical order — numbering controls dependency sequencing.
- **Hard token budget: keep each spec under ~12,000 characters (~3,000 words).**
  The orchestrator auto-compresses specs that exceed this limit by stripping the
  `## Context` section first, then hard-truncating if still too large. Stripped
  specs produce worse model output — stay under the limit by splitting tasks, not
  by trimming important details. If you find yourself writing a Context section
  longer than two short paragraphs, the task is too big: split it.
- **Specs longer than ~16,000 characters automatically route to Llama 4 Scout**
  (30K TPM). That model is weaker on coding than Qwen3 32B — another reason to
  keep specs small.
- **Never reference files that don't exist yet** as dependencies. If task-002
  depends on task-001's output, make that explicit: "src/auth/login.py (created
  by task-001 — run that task first)".
- **Do not include implementation code in the spec.** Function signatures and
  type hints are fine. Full implementations are not — they confuse the model
  and defeat the purpose of cheap code generation.

---

## Test File Requirements

Tests are the definition of "done." The orchestrator runs `pytest tests/ -x`
after every Aider attempt. A task is considered passing only when all tests pass.

### Rules for test files

- **Name must match the spec:** `specs/task-001-user-auth.md` →
  `tests/test-001-user-auth.py`. The number and slug must be identical.
- **Tests must be runnable from the project root** with no additional setup.
  `tests/conftest.py` already adds `src/` to the Python path.
- **Cover the acceptance criteria completely.** If the spec says "tokens expire
  after 24 hours", write a test that asserts that. Don't write happy-path-only tests.
- **Do not test implementation details** (private methods, internal state).
  Test the public interface defined in the spec.
- **Use pytest fixtures for shared setup**, not `unittest.TestCase`.
- **Keep tests self-contained.** Mock external calls (HTTP, DB, filesystem)
  using `pytest-mock` or `unittest.mock`. The pipeline runs without network access.
- **Mark slow tests** with `@pytest.mark.slow` if they take >2 seconds.
  The pipeline runs with `-x` (stop on first failure) — slow tests hurt iteration speed.

### Example test file structure

```python
# tests/test-001-user-auth.py
import pytest
from unittest.mock import patch
from src.auth.login import authenticate_user, create_session_token, validate_token


class TestAuthenticateUser:
    def test_valid_credentials_returns_success(self):
        result = authenticate_user("user@example.com", "correct-password")
        assert result.success is True
        assert result.user is not None
        assert result.error is None

    def test_wrong_password_returns_failure(self):
        result = authenticate_user("user@example.com", "wrong-password")
        assert result.success is False
        assert result.error is not None

    def test_unknown_email_returns_failure(self):
        result = authenticate_user("nobody@example.com", "any-password")
        assert result.success is False


class TestCreateSessionToken:
    def test_returns_non_empty_string(self):
        token = create_session_token(user_id=1)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_tokens_are_unique(self):
        t1 = create_session_token(user_id=1)
        t2 = create_session_token(user_id=1)
        assert t1 != t2


class TestValidateToken:
    def test_valid_token_returns_session(self):
        token = create_session_token(user_id=42)
        session = validate_token(token)
        assert session is not None
        assert session.user_id == 42

    def test_invalid_token_returns_none(self):
        assert validate_token("not-a-real-token") is None

    def test_expired_token_returns_none(self):
        with patch("src.auth.login.datetime") as mock_dt:
            from datetime import datetime, timedelta
            mock_dt.now.return_value = datetime.now() + timedelta(hours=25)
            token = create_session_token(user_id=1)
        assert validate_token(token) is None
```

---

## Task Decomposition Guidelines

When the user describes a feature, decompose it into tasks using this checklist:

**Good task characteristics:**
- Touches exactly one `src/` file (or two if they are tightly coupled, e.g. a
  module + its `__init__.py` export)
- Can be described in one spec file under 500 words
- Has clear pass/fail criteria expressible as pytest assertions
- Has no circular dependencies on other in-progress tasks

**Warning signs to restructure:**
- A task that says "and also update X, Y, Z to use the new module" — split those
  into separate migration tasks numbered after the original
- A task with more than 5 functions to implement — split by logical grouping
- A task where the test setup requires other unfinished tasks to exist — reorder
  or add a stub/mock for the dependency

**Typical decomposition pattern for a new feature:**
```
task-001  Data models / dataclasses (no external dependencies)
task-002  Core business logic (depends on task-001 types)
task-003  Database/storage layer (depends on task-001 types)
task-004  API/service layer (depends on task-002 + task-003)
task-005  Integration / wiring (depends on all above)
```

---

## Reviewing Failed Tasks

When a task lands in `specs/FAILED-task-NNN-name.log`, it means the primary
model tier (Qwen3/Kimi K2/Llama 4 Scout) **and** GPT-OSS 120B both failed after
their full retry budget. Bring in the failure like this:

```
Look at specs/FAILED-task-003-api-endpoints.log and the code in
src/api/endpoints.py on branch task/task-003-api-endpoints.
The primary tier and GPT-OSS 120B both failed to pass the tests.
Diagnose and fix so all tests in tests/test-003-api-endpoints.py pass.
```

When fixing a failed task:
- Check out the task branch first: `git checkout task/task-003-api-endpoints`
- Read the failure log to understand what the model attempted
- Fix the implementation directly — do not regenerate from scratch unless the
  spec itself was wrong
- After fixing, run `pytest tests/test-003-api-endpoints.py -v` to confirm
- Commit your fix: `git add . && git commit -m "fix: task-003 — [what you fixed]"`
- Return to master and re-run `python3 orchestrator.py` — it will detect the passing
  branch and skip it

---

## Checking Your Usage Budget

Run `/status` inside Claude Code before starting a planning session. Budget your
tokens accordingly:

| Phase | Token cost | Priority |
|-------|-----------|----------|
| Generating specs + tests | Medium | High — this is your main job |
| Reviewing specs | ~0 (reading) | Medium |
| Diagnosing failures | Low-Medium | High — only for genuine hard failures |
| Writing implementation code | High | Low — let cheap models do this |

If you're running low on budget, finish the current spec batch and save failure
review for the next session. The orchestrator state persists in git branches.

---

## Project Structure Reference

```
project-root/
├── CLAUDE.md              ← this file
├── README.md              ← full workflow walkthrough
├── orchestrator.py        ← runs the automated pipeline
├── .aider.conf.yml        ← Aider defaults (model, architect mode, etc.)
├── litellm-config.yaml    ← LiteLLM model routing + fallback config
├── .env                   ← API keys (never committed)
├── specs/
│   ├── SpecsReadMe.md     ← human-readable summary of all specs (you generate)
│   ├── task-001-name.md   ← spec files you generate
│   ├── task-002-name.md
│   └── FAILED-task-*.log  ← written by orchestrator on failure
├── tests/
│   ├── conftest.py        ← sys.path setup (do not modify)
│   ├── test-001-name.py   ← test files you generate
│   └── test-002-name.py
└── src/
    ├── __init__.py
    └── ...                ← implementation files (generated by cheap models)
```

---

## Quick Reference — What to Do When

| User says... | You do... |
|---|---|
| "I want to build X" | Decompose into tasks, write specs + tests + SpecsReadMe.md to disk |
| "Review the specs" | Read specs/SpecsReadMe.md first, then flag issues and edit specs directly |
| "Update the readme" | Update specs/SpecsReadMe.md to reflect current spec state |
| "Run the pipeline" | Remind user to run `python3 orchestrator.py` — you don't run it |
| "Fix task NNN" | Check out branch, read failure log, fix implementation, commit |
| "Add a task for Y" | Write a new `specs/task-NNN-y.md` + `tests/test-NNN-y.py`, update SpecsReadMe.md |
| "What's the status?" | Run `git branch --list "task/*"` and `ls specs/FAILED-*.log` |
