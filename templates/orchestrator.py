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
