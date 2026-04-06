import os
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

# Seconds to pause between tasks so Groq's per-minute token window can reset.
INTER_TASK_COOLDOWN = 30

# Content markers used to sanity-check that a model didn't replace a source file
# with garbage (e.g. a shell command or a filename string). Keyed by file extension.
CONTENT_MARKERS = {
    ".py":   ["def ", "class ", "import ", "from "],
    ".java": ["class ", "interface ", "package ", "public ", "import "],
    ".xml":  ["<", "<?xml"],
    ".yml":  [":"],
    ".yaml": [":"],
    ".json": ["{", "["],
}


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

    Uses the LAST occurrence in the output — LiteLLM may retry internally and
    print multiple rate-limit errors; the last one reflects the most recent
    state of the sliding token window.

    Returns seconds as a float, or None if not found.
    """
    matches = re.findall(r"try again in ([0-9.]+)s", stderr_text)
    if matches:
        return float(matches[-1])
    return None


def select_primary_model(spec_text):
    if len(spec_text) > LARGE_CONTEXT_CHARS:
        print("  [large context -- routing directly to Llama 4 Scout]")
        return "groq/meta-llama/llama-4-scout-17b-16e-instruct", None
    return LITELLM_MODEL, LITELLM_BASE


def task_test_file(task_name):
    """
    Derive the pytest test file path from a task name.
    task-001-pom -> tests/test_001_pom.py  (underscores, not hyphens)
    """
    slug = task_name.replace("task-", "", 1).replace("-", "_")
    return f"tests/test_{slug}.py"


def run_tests(task_name):
    """
    Run the task-specific test file instead of the full suite.
    Returns (passed, output). Fails if the test file doesn't exist
    or if pytest collects 0 items (vacuous pass guard).
    """
    test_file = task_test_file(task_name)
    if not Path(test_file).exists():
        return False, f"Test file {test_file} does not exist."

    cmd = ["pytest", test_file, "-x", "--tb=short"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr

    # Guard against vacuous passes — if pytest collected 0 items the task
    # did NOT actually pass, even though the exit code may be 0 (or 5).
    if "no tests ran" in output or "collected 0 items" in output:
        return False, f"Vacuous pass: pytest collected 0 tests from {test_file}.\n\n{output}"

    return result.returncode == 0, output


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
        "--no-show-model-warnings",
        "--no-summarize",
        "--no-auto-lint",
    ]
    if api_base:
        cmd += ["--openai-api-base", api_base, "--openai-api-key", "sk-anything"]

    # Disable LiteLLM's internal retry loop — it retries too aggressively
    # (0.2s, 0.5s, ... 8s) without respecting Groq's "try again in Xs" hint,
    # causing repeated 429s that burn through the token window. We handle all
    # retry logic here with the correct wait times.
    aider_env = os.environ.copy()
    aider_env["LITELLM_NUM_RETRIES"] = "0"

    fallback_wait = 15  # seconds, doubles each retry
    for attempt in range(1, AIDER_MAX_RATE_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=aider_env)
        # Stream output so the user can see aider's progress
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        if result.returncode == 0:
            return

        combined = (result.stdout or "") + (result.stderr or "")

        if "rate_limit_exceeded" in combined or "Rate limit reached" in combined:
            wait = _parse_retry_after(combined)
            if wait:
                wait += 5  # buffer so the sliding window has definitely reset
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


def file_size(path):
    """Return file size in bytes, or 0 if the file doesn't exist."""
    p = Path(path)
    return p.stat().st_size if p.exists() else 0


def _content_looks_valid(target_file):
    """
    Quick sanity check: does the file still look like source code for its type?
    Returns True if the file contains at least one expected marker for its
    extension, or if the extension is unknown (benefit of the doubt).
    """
    ext = Path(target_file).suffix.lower()
    markers = CONTENT_MARKERS.get(ext)
    if not markers:
        return True  # unknown extension — skip check

    p = Path(target_file)
    if not p.exists() or p.stat().st_size == 0:
        return True  # empty file is handled by size check, not here

    try:
        content = p.read_text(errors="replace")
    except Exception:
        return True  # can't read — don't block on I/O errors

    return any(marker in content for marker in markers)


def check_regression(target_file, baseline_size):
    """
    Return True if the target file looks corrupted after a model edit.

    Two checks:
    1. Size regression — file shrank by >80% vs baseline (catches stubs).
    2. Content sanity — file no longer contains any expected language markers
       (catches cases where a model replaces Java with a shell command, etc.).
    """
    # Size check (only meaningful when baseline was non-trivial)
    if baseline_size >= 50:
        current = file_size(target_file)
        if current < baseline_size * 0.2:
            return True

    # Content marker check
    if not _content_looks_valid(target_file):
        return True

    return False


def revert_regression(target_file, baseline_size):
    """Undo the last commit and print a warning."""
    current = file_size(target_file)
    if baseline_size > 0:
        pct = int((1 - current / baseline_size) * 100)
        print(
            f"  [REGRESSION GUARD] {target_file} shrank from {baseline_size}B to {current}B "
            f"(>{pct}% reduction) -- reverting commit."
        )
    else:
        print(
            f"  [REGRESSION GUARD] {target_file} content failed sanity check -- reverting commit."
        )
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=True)


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
        passed, output = run_tests(task_name)
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

    # Snapshot target file size before any model writes, so we can detect
    # regressions where a model replaces a real file with a stub or stray text.
    baseline_size = file_size(target_file)

    primary_model, api_base = select_primary_model(spec_text)
    model_label = "Llama 4 Scout" if api_base is None else "Qwen3/Kimi K2/Scout via LiteLLM"

    # --- Stage 1: Primary tier ---
    print(f"Stage 1: {model_label} ({PRIMARY_RETRIES} attempts)")
    for attempt in range(1, PRIMARY_RETRIES + 1):
        print(f"  Attempt {attempt}/{PRIMARY_RETRIES}...")
        if attempt == 1:
            run_aider(primary_model, spec_text, target_file, api_base)
        else:
            _, output = run_tests(task_name)
            run_aider(primary_model,
                f"Tests failed. Output:\n{output}\nFix the code to pass all tests.",
                target_file, api_base)
        if check_regression(target_file, baseline_size):
            revert_regression(target_file, baseline_size)
            continue
        baseline_size = max(baseline_size, file_size(target_file))
        passed, _ = run_tests(task_name)
        if passed:
            print(f"PASSED (Stage 1, attempt {attempt}): {task_name}")
            subprocess.run(["git", "checkout", default_branch], check=True)
            return "passed"

    # --- Stage 2: Escalation (GPT-OSS 120B, direct -- never via LiteLLM fallback) ---
    print(f"Stage 2: GPT-OSS 120B ({ESCALATION_RETRIES} attempts)")
    for attempt in range(1, ESCALATION_RETRIES + 1):
        print(f"  Attempt {attempt}/{ESCALATION_RETRIES}...")
        _, output = run_tests(task_name)
        run_aider(ESCALATION_MODEL,
            f"Previous model failed. Tests output:\n{output}\nAnalyze carefully and fix.",
            target_file)
        if check_regression(target_file, baseline_size):
            revert_regression(target_file, baseline_size)
            continue
        baseline_size = max(baseline_size, file_size(target_file))
        passed, output = run_tests(task_name)
        if passed:
            print(f"PASSED (GPT-OSS 120B, attempt {attempt}): {task_name}")
            subprocess.run(["git", "checkout", default_branch], check=True)
            return "passed"

    # --- Stage 3: Flag for Claude review ---
    _, output = run_tests(task_name)
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

    for i, spec in enumerate(specs):
        outcome = run_task(spec, default_branch)
        results[outcome].append(spec.stem)

        # Cooldown between tasks so Groq's per-minute token window can reset.
        # Skip after the last task or if the task was already done (skipped).
        if i < len(specs) - 1 and outcome != "skipped" and INTER_TASK_COOLDOWN > 0:
            print(f"  [cooldown: sleeping {INTER_TASK_COOLDOWN}s between tasks]")
            time.sleep(INTER_TASK_COOLDOWN)

    print_summary(results, default_branch)
