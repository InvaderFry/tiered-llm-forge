#!/bin/bash
# new-project.sh — Bootstrap a new tiered LLM coding workflow project
# Run from ~/projects/: bash new-project.sh
# Creates: ~/projects/my-project-YYYYMMDD-HHMMSS/

set -e

# ─── Project name ────────────────────────────────────────────────────────────
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
PROJECT_NAME="my-project-$TIMESTAMP"
PROJECT_DIR="$HOME/projects/$PROJECT_NAME"

echo ""
echo "=================================================="
echo "  Tiered LLM Workflow — New Project Bootstrap"
echo "  Project: $PROJECT_NAME"
echo "=================================================="
echo ""

# ─── Create folder structure ─────────────────────────────────────────────────
mkdir -p "$PROJECT_DIR"/{specs,tests,src}
cd "$PROJECT_DIR"

git init
touch src/__init__.py

cat > tests/conftest.py << 'EOF'
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
EOF

echo "✔  Folder structure created"

# ─── .env (secrets placeholder) ──────────────────────────────────────────────
cat > .env << 'EOF'
# API Keys — fill these in before running the pipeline
# Never commit this file to git.
GROQ_API_KEY=your-groq-key-here
# GOOGLE_API_KEY=your-google-ai-studio-key-here
EOF

echo "✔  .env created (fill in your keys before running the pipeline)"

# ─── .gitignore ──────────────────────────────────────────────────────────────
cat > .gitignore << 'EOF'
.env
__pycache__/
*.pyc
.aider*
EOF

echo "✔  .gitignore created"

# ─── .aider.conf.yml ─────────────────────────────────────────────────────────
cat > .aider.conf.yml << 'EOF'
model: groq/qwen/qwen3-32b
editor-model: groq/qwen/qwen3-32b
weak-model: groq/llama-3.1-8b-instant
auto-commits: true
yes-always: true
architect: true
auto-test: true
test-cmd: pytest tests/ -x
EOF

echo "✔  .aider.conf.yml created"

# ─── litellm-config.yaml ─────────────────────────────────────────────────────
cat > litellm-config.yaml << 'EOF'
model_list:
  - model_name: cheap-coder
    litellm_params:
      model: groq/qwen/qwen3-32b
      api_key: os.environ/GROQ_API_KEY
  - model_name: cheap-coder
    litellm_params:
      model: groq/moonshotai/kimi-k2-instruct
      api_key: os.environ/GROQ_API_KEY
  - model_name: cheap-coder
    litellm_params:
      model: groq/meta-llama/llama-4-scout-17b-16e-instruct
      api_key: os.environ/GROQ_API_KEY

litellm_settings:
  fallbacks: [{"cheap-coder": ["cheap-coder"]}]
  num_retries: 2
EOF

echo "✔  litellm-config.yaml created"

# ─── orchestrator.py ─────────────────────────────────────────────────────────
cat > orchestrator.py << 'PYEOF'
import re
import subprocess
import time
from pathlib import Path

SPECS_DIR = Path("specs")
WEAK_MODEL = "groq/llama-3.1-8b-instant"
ESCALATION_MODEL = "groq/openai/gpt-oss-120b"

# LiteLLM proxy handles: Qwen3 32B -> Kimi K2 -> Llama 4 Scout (rate-limit fallback)
LITELLM_MODEL = "openai/cheap-coder"
LITELLM_BASE = "http://localhost:4000"

# Context size threshold for proactive Llama 4 Scout selection (~4K tokens)
LARGE_CONTEXT_CHARS = 16_000

# Spec compression: target ~3K tokens = ~12K chars. Specs over this get Context
# section stripped first, then hard-truncated with a warning if still too large.
SPEC_CHAR_SOFT_LIMIT = 12_000   # strip ## Context section above this
SPEC_CHAR_HARD_LIMIT = 16_000   # truncate above this (routes to Scout anyway, but smaller is faster)

PRIMARY_RETRIES = 3
ESCALATION_RETRIES = 2
# Max attempts per run_aider call before giving up on that single invocation
AIDER_MAX_RATE_RETRIES = 4
TEST_CMD = "pytest tests/ -x --tb=short"


def compress_spec(spec_text, task_name):
    """
    Reduce spec size to stay within Groq TPM limits.

    Strategy:
    1. If over SPEC_CHAR_SOFT_LIMIT, strip the ## Context section (prose explanation
       that cheap models don't need — they need types and constraints, not backstory).
    2. If still over SPEC_CHAR_HARD_LIMIT, hard-truncate with a visible warning so
       the operator knows something was cut.

    Returns the (possibly compressed) spec string.
    """
    if len(spec_text) <= SPEC_CHAR_SOFT_LIMIT:
        return spec_text

    # Step 1: strip ## Context section
    compressed = re.sub(
        r"## Context\n.*?(?=\n##|\Z)",
        "",
        spec_text,
        flags=re.DOTALL,
    ).strip()

    if len(compressed) <= SPEC_CHAR_HARD_LIMIT:
        print(f"  [spec compressed: stripped ## Context section, {len(spec_text)} -> {len(compressed)} chars]")
        return compressed

    # Step 2: hard truncate
    truncated = compressed[:SPEC_CHAR_HARD_LIMIT]
    print(f"  [WARNING: spec hard-truncated to {SPEC_CHAR_HARD_LIMIT} chars for {task_name} -- split this task if possible]")
    return truncated


def _parse_retry_after(stderr_text):
    """
    Extract the retry-after wait time from a Groq 429 error message.
    Groq includes 'Please try again in Xs.' in the error body.
    Returns seconds as a float, or None if not found.
    """
    match = re.search(r"try again in ([0-9.]+)s", stderr_text)
    if match:
        return float(match.group(1))
    return None


def select_primary_model(spec_text):
    if len(spec_text) > LARGE_CONTEXT_CHARS:
        print("  [large context -- routing directly to Llama 4 Scout]")
        return "groq/meta-llama/llama-4-scout-17b-16e-instruct", None
    return LITELLM_MODEL, LITELLM_BASE


def run_aider(model, message, target_file, api_base=None):
    """
    Run aider with exponential backoff on Groq 429 rate-limit errors.

    Groq 429s include 'Please try again in Xs.' in the error body. We parse
    that value and sleep exactly that long before retrying, instead of the
    useless 0.5s LiteLLM default. Falls back to exponential backoff (15s,
    30s, 60s...) if the wait time can't be parsed.
    """
    cmd = [
        "aider",
        "--model", model,
        "--message", message,
        "--file", target_file,
        "--weak-model", WEAK_MODEL,
        "--yes-always",
        "--auto-commits",
        "--no-stream",
    ]
    if api_base:
        cmd += ["--openai-api-base", api_base, "--openai-api-key", "sk-anything"]

    fallback_wait = 15  # seconds, doubles each retry
    for attempt in range(1, AIDER_MAX_RATE_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=False, text=True, check=False)
        if result.returncode == 0:
            return

        # Capture stderr to check for rate limit
        probe = subprocess.run(cmd, capture_output=True, text=True, check=False)
        combined = (probe.stdout or "") + (probe.stderr or "")

        if "rate_limit_exceeded" in combined or "Rate limit reached" in combined:
            wait = _parse_retry_after(combined)
            if wait:
                wait += 2  # small buffer so the window has definitely reset
                print(f"  [rate limit: sleeping {wait:.1f}s as instructed by Groq (attempt {attempt}/{AIDER_MAX_RATE_RETRIES})]")
            else:
                wait = fallback_wait
                print(f"  [rate limit: sleeping {wait}s (exponential backoff, attempt {attempt}/{AIDER_MAX_RATE_RETRIES})]")
                fallback_wait = min(fallback_wait * 2, 120)

            if attempt < AIDER_MAX_RATE_RETRIES:
                time.sleep(wait)
                continue

        # Non-rate-limit error or exhausted retries — give up on this invocation
        return


def run_tests():
    result = subprocess.run(TEST_CMD.split(), capture_output=True, text=True)
    return result.returncode == 0, result.stdout + result.stderr


def parse_target_file(spec_text):
    lines = spec_text.splitlines()
    for i, line in enumerate(lines):
        if "## Target file" in line and i + 1 < len(lines):
            return lines[i + 1].strip()
    return ""


def get_default_branch():
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True
    )
    branch = result.stdout.strip()
    if branch and not branch.startswith("task/"):
        return branch
    result2 = subprocess.run(
        ["git", "config", "--get", "init.defaultBranch"],
        capture_output=True, text=True
    )
    return result2.stdout.strip() or "master"


def ensure_default_branch_exists():
    result = subprocess.run(["git", "log", "--oneline", "-1"], capture_output=True, text=True)
    if result.returncode != 0:
        subprocess.run(["git", "commit", "--allow-empty", "-m", "chore: initial commit"], check=True)


def branch_exists(branch_name):
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def run_task(spec_file, default_branch):
    """
    Run a task through the full model escalation ladder.

    Re-run behaviour:
    - Branch exists + tests pass  -> already done, skip (return 'skipped')
    - Branch exists + tests fail  -> previous failure, go straight to Claude review
    - Branch does not exist       -> normal first run
    """
    task_name = spec_file.stem
    spec_text = spec_file.read_text()
    target_file = parse_target_file(spec_text)
    branch_name = f"task/{task_name}"

    print(f"\n{'='*50}\n{task_name}\n{'='*50}")

    # Compress spec before any model call to stay within Groq TPM limits.
    # compress_spec strips the ## Context prose section first (soft limit),
    # then hard-truncates if still too large (hard limit). Size is checked
    # AFTER compression for routing purposes.
    spec_text = compress_spec(spec_text, task_name)

    # --- Handle re-run: branch already exists ---
    if branch_exists(branch_name):
        print(f"  Branch '{branch_name}' already exists -- checking previous result...")
        subprocess.run(["git", "checkout", branch_name], check=True)
        passed, output = run_tests()
        if passed:
            print(f"  Already passing -- skipping.")
            subprocess.run(["git", "checkout", default_branch], check=True)
            return "skipped"
        else:
            print(f"  Previously failed -- escalating straight to Claude review.")
            fail_log = SPECS_DIR / f"FAILED-{task_name}.log"
            fail_log.write_text(
                f"Previously attempted -- still failing on re-run.\n\n{output}"
            )
            subprocess.run(["git", "checkout", default_branch], check=True)
            return "failed"

    # --- Normal first run ---
    subprocess.run(["git", "checkout", "-b", branch_name], check=True)

    primary_model, api_base = select_primary_model(spec_text)
    model_label = "Llama 4 Scout" if api_base is None else "Qwen3/Kimi K2/Scout via LiteLLM"

    # --- Stage 1: Primary tier ---
    print(f"Stage 1: {model_label} ({PRIMARY_RETRIES} attempts)")
    for attempt in range(1, PRIMARY_RETRIES + 1):
        print(f"  Attempt {attempt}/{PRIMARY_RETRIES}...")
        if attempt == 1:
            run_aider(primary_model, spec_text, target_file, api_base)
        else:
            _, output = run_tests()
            run_aider(primary_model,
                f"Tests failed. Output:\n{output}\nFix the code to pass all tests.",
                target_file, api_base)
        passed, _ = run_tests()
        if passed:
            print(f"PASSED (Stage 1, attempt {attempt}): {task_name}")
            subprocess.run(["git", "checkout", default_branch], check=True)
            return "passed"

    # --- Stage 2: Escalation (GPT-OSS 120B, direct -- never via LiteLLM fallback) ---
    print(f"Stage 2: GPT-OSS 120B ({ESCALATION_RETRIES} attempts)")
    for attempt in range(1, ESCALATION_RETRIES + 1):
        print(f"  Attempt {attempt}/{ESCALATION_RETRIES}...")
        _, output = run_tests()
        run_aider(ESCALATION_MODEL,
            f"Previous model failed. Tests output:\n{output}\nAnalyze carefully and fix.",
            target_file)
        passed, output = run_tests()
        if passed:
            print(f"PASSED (GPT-OSS 120B, attempt {attempt}): {task_name}")
            subprocess.run(["git", "checkout", default_branch], check=True)
            return "passed"

    # --- Stage 3: Flag for Claude review ---
    _, output = run_tests()
    fail_log = SPECS_DIR / f"FAILED-{task_name}.log"
    fail_log.write_text(
        f"Failed after primary tier ({PRIMARY_RETRIES}x) + GPT-OSS 120B ({ESCALATION_RETRIES}x).\n\n{output}"
    )
    print(f"ESCALATE TO CLAUDE: {task_name}")
    subprocess.run(["git", "checkout", default_branch], check=True)
    return "failed"


def print_summary(results, default_branch):
    passed  = results["passed"]
    failed  = results["failed"]
    skipped = results["skipped"]

    print(f"\n{'='*50}")
    print(f"PASSED:  {len(passed)}")
    print(f"SKIPPED (already passing): {len(skipped)}")
    print(f"FAILED:  {len(failed)}")

    if failed:
        print(f"\n{'='*50}")
        print("PHASE 4 -- Claude review needed for these failures:")
        for f in failed:
            log = SPECS_DIR / f"FAILED-{f}.log"
            print(f"\n  Task:   {f}")
            print(f"  Branch: task/{f}")
            print(f"  Log:    {log}")
            print(f"  Prompt for Claude Code:")
            print(f"    > Look at {log} and the code on branch task/{f}.")
            print(f"    > Diagnose and fix so the tests pass.")
        print(f"\nAfter Claude fixes each failure, re-run this script.")
        print("Fixed tasks will be detected as passing and skipped automatically.")

    else:
        print(f"\n{'='*50}")
        print("All tasks passing -- ready to merge.")
        print("Review and squash-merge each passing branch:\n")
        for t in passed + skipped:
            print(f"  git checkout {default_branch}")
            print(f"  git diff {default_branch}..task/{t}   # review first")
            print(f"  git merge --squash task/{t}")
            print(f"  git commit -m 'feat: {t}'")
            print(f"  git branch -d task/{t}")
            print()


if __name__ == "__main__":
    ensure_default_branch_exists()
    default_branch = get_default_branch()
    specs = sorted(SPECS_DIR.glob("task-*.md"))

    if not specs:
        print("No spec files found in specs/. Add task-*.md files and re-run.")
        exit(0)

    results = {"passed": [], "failed": [], "skipped": []}

    for spec in specs:
        outcome = run_task(spec, default_branch)
        results[outcome].append(spec.stem)

    print_summary(results, default_branch)
PYEOF

echo "✔  orchestrator.py created"

# ─── README.md ───────────────────────────────────────────────────────────────
cat > README.md << READMEEOF
# $PROJECT_NAME

Tiered LLM coding workflow project.
Claude plans and reviews. Free Groq models implement.

---

## Phase 1: Set Your API Keys

Edit \`.env\` in the project root before running anything:

\`\`\`bash
nano .env
\`\`\`

Fill in your values:

\`\`\`env
GROQ_API_KEY=your-groq-key-here
# GOOGLE_API_KEY=your-google-ai-studio-key-here   # optional
\`\`\`

Get keys from:
- **Groq:** https://console.groq.com → API Keys
- **Google AI Studio (optional):** https://aistudio.google.com → Get API Key

> ⚠️ \`.env\` is in \`.gitignore\` — it will never be committed. Never paste keys
> into any other file that gets committed.

Load your keys into the shell before running the pipeline:

\`\`\`bash
export \$(cat .env | grep -v '#' | xargs)
\`\`\`

Add that line to \`~/.bashrc\` to load keys automatically on every terminal open.

---

## Phase 2: Start LiteLLM (once per reboot)

LiteLLM is the routing proxy that handles Groq model fallback. Start it with Docker:

\`\`\`bash
export \$(cat .env | grep -v '#' | xargs)

docker run -d --name litellm -p 4000:4000 \\
  -v \$(pwd)/litellm-config.yaml:/app/config.yaml \\
  -e GROQ_API_KEY=\$GROQ_API_KEY \\
  ghcr.io/berriai/litellm:main-latest \\
  --config /app/config.yaml
\`\`\`

After the first time, just run:

\`\`\`bash
docker start litellm
\`\`\`

Verify it's running:

\`\`\`bash
curl http://localhost:4000/health
\`\`\`

---

## Phase 3: Plan with Claude Code

Open Claude Code in this project directory:

\`\`\`bash
claude
\`\`\`

CLAUDE.md is already loaded — Claude Code knows the spec format, file locations,
naming conventions, and its role. You just need to describe what you want to build.

**Basic prompt (enough for most features):**

\`\`\`
I want to build [feature]. Generate the specs and tests.
\`\`\`

**Better prompt (give context, get better decomposition):**

\`\`\`
I want to build [feature]. It should [key behaviours]. Generate the specs and tests.
\`\`\`

Example:

\`\`\`
I want to build a rate limiter for an API. It should track requests per user
per minute using a sliding window, reject over-limit requests with a 429
response, and persist state in Redis so it survives restarts. Generate the
specs and tests.
\`\`\`

**Other useful prompts to keep handy:**

After specs are generated, before running the pipeline:
\`\`\`
Review the specs you just created. Check for dependency ordering issues,
tasks that touch more than one file, and tests that might not be self-contained.
\`\`\`

Adding to an existing codebase:
\`\`\`
I want to add [feature] to the existing codebase. Read src/ first, then
generate specs that follow the same patterns already in use.
\`\`\`

Fixing a failure:
\`\`\`
Fix task-003. The log is at specs/FAILED-task-003-name.log.
\`\`\`

Use \`/status\` inside Claude Code to check your remaining Pro usage budget.
Planning is your most valuable use of Claude tokens — describe the feature
well and let CLAUDE.md handle the process.

---

## Phase 4: Review Specs (Manual Checkpoint)

Before running the pipeline, read through the specs Claude generated:

- Each task touches only one file (or a minimal set)
- Tests actually cover the acceptance criteria
- Specs include enough context about existing code patterns
- Tasks are ordered correctly (dependencies first)

Edit spec files directly if anything looks wrong. This is your last cheap
chance to catch architectural mistakes before cheap models start generating code.

---

## Phase 5: Run the Pipeline

\`\`\`bash
export \$(cat .env | grep -v '#' | xargs)
python3 orchestrator.py
\`\`\`

The orchestrator loops through every \`specs/task-*.md\` file and:

1. Creates a \`task/task-NNN-name\` git branch
2. Runs Aider with **Qwen3 32B → Kimi K2 → Llama 4 Scout** via LiteLLM (3 attempts)
3. Escalates to **GPT-OSS 120B** directly if primary tier fails (2 attempts)
4. Writes a \`specs/FAILED-task-NNN-name.log\` if all attempts fail
5. Returns to the default branch and moves to the next task

**Re-running is safe.** Tasks with passing branches are skipped automatically.
Tasks with failing branches go straight to Claude review without wasting retries.

### Model routing

| Condition | Model used |
|-----------|-----------|
| Normal task | Qwen3 32B via LiteLLM (falls back to Kimi K2 → Llama 4 Scout on 429) |
| Spec > ~4K tokens | Llama 4 Scout directly (30K TPM handles large context) |
| Primary tier fails all retries | GPT-OSS 120B directly (never via LiteLLM) |
| Commit messages | Llama 3.1 8B (14.4K RPD — never burns primary quota) |

---

## Phase 6: Review Failures with Claude

After the pipeline finishes, check for failures:

\`\`\`bash
ls specs/FAILED-*.log
\`\`\`

For each failure, open Claude Code and paste this prompt:

\`\`\`
Look at specs/FAILED-task-NNN-name.log and the code on branch task/task-NNN-name.
Both the primary models and GPT-OSS 120B failed to pass the tests.
Diagnose and fix so the tests pass.
\`\`\`

After Claude fixes the code on the task branch, re-run the pipeline.
The fixed task will be detected as passing and skipped automatically.

---

## Phase 7: Merge Passing Tasks

After the pipeline prints "All tasks passing", squash-merge each branch:

\`\`\`bash
git checkout master                            # or your default branch
git diff master..task/task-001-feature-name   # review the changes first
git merge --squash task/task-001-feature-name
git commit -m "feat: implement feature name"
git branch -d task/task-001-feature-name
\`\`\`

The orchestrator prints the exact commands for each branch when everything passes.

---

## Daily Workflow Summary

\`\`\`
1. cd ~/projects/$PROJECT_NAME
2. docker start litellm                         # if not already running
3. export \$(cat .env | grep -v '#' | xargs)
4. claude                                       # open Claude Code
   > "I want to build [feature]. Generate the specs and tests."
   > "Review the specs you just created."      # optional sanity check
5. Review specs/ manually                       # Phase 4: catch anything obvious
6. python3 orchestrator.py                      # Phase 5: run pipeline
7. For failures: claude                         # Phase 6: fix failures
   > "Fix task-NNN. Log is at specs/FAILED-task-NNN-name.log."
8. python3 orchestrator.py                      # re-run until all pass
9. Squash-merge passing branches                # Phase 7: merge
\`\`\`

**Expected Claude Pro usage:** ~30–45 minutes of interactive Claude Code per day
(planning + failure review). The pipeline itself uses zero Claude tokens.

---

## Cheat Sheet

| What | Command |
|------|---------|
| Load env vars | \`export \$(cat .env \| grep -v '#' \| xargs)\` |
| Start LiteLLM | \`docker start litellm\` |
| Open Claude Code | \`claude\` |
| Check Claude usage | \`/status\` inside Claude Code |
| Run pipeline | \`python3 orchestrator.py\` |
| Check failures | \`ls specs/FAILED-*.log\` |
| See task branches | \`git branch --list "task/*"\` |
| Diff a task branch | \`git diff master..task/task-001-name\` |

---

## Troubleshooting

**Aider can't find Groq models**
→ Run \`export \$(cat .env | grep -v '#' | xargs)\` first. Add to \`~/.bashrc\` to make it permanent.

**LiteLLM container exits immediately**
→ Run \`docker logs litellm\`. Most common cause: missing \`GROQ_API_KEY\` env var or malformed \`litellm-config.yaml\`.

**pytest can't import src modules**
→ Make sure \`src/__init__.py\` exists and you're running pytest from the project root.

**Rate limit 429 from Groq**
→ LiteLLM falls back automatically to Kimi K2 → Llama 4 Scout. If all three are exhausted, wait a few minutes — Groq limits reset hourly.

**Task branch already exists error**
→ This shouldn't happen anymore. The orchestrator detects existing branches and either skips (if passing) or routes to Claude review (if failing).

**Claude Code hits usage limits**
→ Use \`/status\` to check budget before planning. Work during off-peak hours (before 5 AM PT or after 11 AM PT) to avoid tighter limits.
READMEEOF

echo "✔  README.md created"

# ─── CLAUDE.md (Claude Code planning instructions) ───────────────────────────
cat > CLAUDE.md << 'CLAUDEEOF'
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
CLAUDEEOF

echo "✔  CLAUDE.md created"

# ─── Initial git commit ───────────────────────────────────────────────────────
git add .
git commit -m "chore: project scaffold"

echo ""
echo "=================================================="
echo "  Project ready: $PROJECT_DIR"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your GROQ_API_KEY"
echo "  2. Start LiteLLM:  docker start litellm"
echo "     (or start fresh: see README Phase 2)"
echo "  3. Open Claude Code: cd $PROJECT_DIR && claude"
echo "  4. See README.md for the full workflow"
echo ""
