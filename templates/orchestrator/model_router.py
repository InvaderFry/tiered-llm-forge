"""Model selection, fallback routing, and rate limit handling."""

import os
import re
import subprocess
import time

from .config import get_config, get_tier

# Max retries within a single aider invocation for rate limit errors
AIDER_MAX_RATE_RETRIES = 4


def select_model_for_spec(spec_text):
    """
    Choose the right model based on spec size.

    Returns the model string (e.g., 'groq/qwen/qwen3-32b').
    Large specs route directly to the large context model.

    TODO: The large-context branch below is dead code. compress_spec() hard-caps
    specs at hard_limit_chars (16,000) before this function is called, so
    len(spec_text) > large_context_chars (also 16,000) can never be true.
    Remove large_context_model and large_context_chars from models.yaml and
    delete the branch below once confirmed unnecessary.
    """
    cfg = get_config()
    large_threshold = cfg.get("large_context_chars", 16_000)

    if len(spec_text) > large_threshold:
        model = cfg["large_context_model"]
        print(f"  [large context -- routing directly to {model}]")
        return model

    # Return first model in primary tier
    primary = get_tier("primary")
    return primary["models"][0]


def get_fallback_models(current_model, tier_name):
    """
    Return remaining models in the tier after the current one.

    If current_model is not in the tier, returns all models.
    """
    tier = get_tier(tier_name)
    models = tier["models"]
    try:
        idx = models.index(current_model)
        return models[idx + 1:]
    except ValueError:
        return models


def _parse_retry_after(stderr_text):
    """
    Extract the retry-after wait time from a Groq 429 error message.

    Uses the LAST occurrence — LiteLLM may print multiple rate-limit errors;
    the last one reflects the most recent state.

    Returns seconds as a float, or None if not found.
    """
    matches = re.findall(r"try again in ([0-9.]+)s", stderr_text)
    if matches:
        return float(matches[-1])
    return None


def run_aider(model, message, target_file):
    """
    Run aider with a specific model, handling rate limit retries.

    Returns True if aider exited successfully, False otherwise.
    """
    cfg = get_config()
    weak_model = cfg.get("weak_model", "groq/llama-3.1-8b-instant")

    cmd = [
        "aider",
        "--model", model,
        "--message", message,
        "--file", target_file,
        "--weak-model", weak_model,
        "--yes-always",
        "--auto-commits",
        "--no-stream",
        "--no-show-model-warnings",
        "--no-auto-lint",
    ]

    # Disable LiteLLM's internal retry loop — we handle retries with correct wait times
    aider_env = os.environ.copy()
    aider_env["LITELLM_NUM_RETRIES"] = "0"

    fallback_wait = 15
    for attempt in range(1, AIDER_MAX_RATE_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=aider_env)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        if result.returncode == 0:
            return True

        combined = (result.stdout or "") + (result.stderr or "")

        if "rate_limit_exceeded" in combined or "Rate limit reached" in combined:
            # "Request too large" means the request itself exceeds the TPM cap —
            # no amount of waiting will help. Fail immediately so the tier
            # fallback can try the next model.
            if "Request too large" in combined:
                print(f"  [request too large for model {model} TPM cap — falling back]")
                return False

            wait = _parse_retry_after(combined)
            if wait:
                wait += 5  # buffer for sliding window reset
                print(f"  [rate limit: sleeping {wait:.1f}s as instructed by Groq (attempt {attempt}/{AIDER_MAX_RATE_RETRIES})]")
            else:
                wait = fallback_wait
                print(f"  [rate limit: sleeping {wait}s (exponential backoff, attempt {attempt}/{AIDER_MAX_RATE_RETRIES})]")
                fallback_wait = min(fallback_wait * 2, 120)

            if attempt < AIDER_MAX_RATE_RETRIES:
                time.sleep(wait)
                continue

        # Non-rate-limit error or exhausted retries
        return False

    return False


def run_with_tier_fallback(tier_name, message, target_file, start_model=None):
    """
    Try all models in a tier with fallback.

    Tries each model in the tier. On rate limit exhaustion for one model,
    moves to the next. Returns (success, model_used).
    """
    tier = get_tier(tier_name)
    models = tier["models"]

    if start_model and start_model in models:
        idx = models.index(start_model)
        models = models[idx:]

    for model in models:
        print(f"  Trying {model}...")
        success = run_aider(model, message, target_file)
        if success:
            return True, model

    return False, None
