# Spec Format Reference

## Template

Every spec file must follow this structure. The orchestrator's `spec_parser.py`
reads the YAML frontmatter to determine target file, test file, and dependencies.

```markdown
---
task: "001"
name: "short-descriptive-name"
target: src/module/filename.py
test: tests/test_001_short_descriptive_name.py
dependencies: []
---

# Task 001: Short Descriptive Name

## Functions to implement
- `function_name(param: type, param2: type) -> ReturnType`
- `another_function(param: type) -> ReturnType`

## Data structures / types
\```python
from dataclasses import dataclass

@dataclass
class ExampleResult:
    success: bool
    value: str
    error: str | None = None
\```

## Constraints
- Specific library to use (e.g., "use bcrypt for password hashing")
- Patterns to follow (e.g., "follow the pattern in src/existing/module.py")
- Performance or correctness requirements

## Context
One short paragraph explaining WHY this task exists and how it fits into the
larger feature. This section gets stripped by the orchestrator to save tokens.
Keep important technical details in other sections.
```

---

## YAML Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `task` | Yes | Zero-padded task number: "001", "002", etc. |
| `name` | Yes | Hyphenated short name matching the filename |
| `target` | Yes | Path to the file this task creates/modifies |
| `test` | Yes | Path to the test file for this task |
| `dependencies` | No | List of task names this depends on (e.g., `["task-001-models"]`) |

---

## Critical Rules

- **One target file per task.** If a feature needs multiple files, split into
  multiple tasks with dependency ordering.
- **Zero-padded three digits:** `task-001`, `task-002`, etc.
- **Hard token budget: ~12,000 characters (~3,000 words).** The orchestrator
  strips `## Context` first, then hard-truncates. Stay under the limit by
  splitting tasks, not trimming details.
- **Specs over ~16,000 characters route to a weaker model** (Llama 4 Scout).
  Another reason to keep specs small.
- **Never reference files that don't exist yet** as dependencies. If task-002
  depends on task-001's output, declare it in the frontmatter `dependencies`.
- **No implementation code in specs.** Function signatures and type hints only.

---

## SpecsReadMe.md Template

```markdown
# Specs — [Feature Name] — [Date]

## What we're building
One paragraph summary. What problem does it solve?

## Architecture decisions
- Why library X was chosen over Y
- Why something was split into N tasks
- Key tradeoffs

## Task summary

| Task | File | What it does | Depends on |
|------|------|-------------|------------|
| task-001-models | src/models/user.py | User and AuthResult dataclasses | — |
| task-002-auth | src/auth/login.py | authenticate_user, token handling | task-001 |
| task-003-api | src/api/endpoints.py | POST /login and /logout routes | task-002 |

## Pre-flight checklist
- [ ] Dependencies have lower task numbers
- [ ] Each task touches only one file
- [ ] Tests are self-contained (no network, no unfinished deps)
- [ ] No spec references a file that doesn't exist yet
- [ ] Estimated total: N tasks

## Context stripped from specs
Notes on anything left out of specs to save tokens but useful for human review.
```

---

## Test File Rules

- **Name must match spec** (with underscores): `task-001-user-auth` →
  `test_001_user_auth.py`
- **Runnable from project root** with no setup. `conftest.py` handles imports.
- **Cover acceptance criteria completely.** Not happy-path-only.
- **Test public interface**, not internals.
- **Use pytest fixtures**, not `unittest.TestCase`.
- **Self-contained.** Mock external calls with `unittest.mock`.
- **Mark slow tests** `@pytest.mark.slow` if >2 seconds.

### Example

```python
# tests/test_001_user_auth.py
import pytest
from src.auth.login import authenticate_user, create_session_token

class TestAuthenticateUser:
    def test_valid_credentials(self):
        result = authenticate_user("user@example.com", "correct-password")
        assert result.success is True

    def test_wrong_password(self):
        result = authenticate_user("user@example.com", "wrong")
        assert result.success is False
        assert result.error is not None
```

---

## Task Decomposition Guidelines

**Good task characteristics:**
- Touches exactly one file
- Under 500 words
- Clear pass/fail criteria as pytest assertions
- No circular dependencies

**Warning signs:**
- "Also update X, Y, Z" → split into migration tasks
- More than 5 functions → split by grouping
- Test setup needs unfinished tasks → reorder or add mocks

**Typical pattern:**
```
task-001  Data models (no deps)
task-002  Core business logic (depends on 001)
task-003  Storage layer (depends on 001)
task-004  API/service layer (depends on 002 + 003)
task-005  Integration wiring (depends on all above)
```
