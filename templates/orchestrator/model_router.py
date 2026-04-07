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
    Choose the starting model for a task.

    Always returns the first model in the primary tier — the tier
    fallback machinery handles model rotation from there. The previous
    "large context" branch was unreachable (compress_spec hard-capped
    at the same threshold) and the spec is now attached as a read-only
    file rather than embedded in the prompt, so spec length no longer
    drives model choice. ``spec_text`` is kept on the signature so
    future heuristics (e.g. routing by language or function count)
    can be layered in without changing call sites.
    """
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


# Aider stdout reports usage on lines like:
#   Tokens: 1.2k sent, 234 received.
#   Cost: $0.0034 message, $0.0102 session.
# We grab the per-message numbers and sum across every "Tokens:" line in
# a single aider invocation, since aider may make several model calls
# per --message (commit message generation, retries, etc.).
_TOKENS_RE = re.compile(
    r"Tokens?:\s*([\d.]+)\s*([kKmM]?)\s*sent[^,]*,\s*([\d.]+)\s*([kKmM]?)\s*received",
    re.IGNORECASE,
)
_COST_RE = re.compile(
    r"Cost:\s*\$([\d.]+)\s*message",
    re.IGNORECASE,
)

_UNIT_MULT = {"": 1, "k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}


def _parse_usage(output):
    """Extract aggregated token/cost stats from a single aider invocation.

    Returns a dict with ``tokens_sent``, ``tokens_received``, and
    ``cost_usd`` (always present, zero when nothing matched). Sums
    across multiple usage lines because one --message can trigger
    several model calls.
    """
    sent = 0
    received = 0
    cost = 0.0

    for match in _TOKENS_RE.finditer(output or ""):
        s_num, s_unit, r_num, r_unit = match.groups()
        try:
            sent += int(float(s_num) * _UNIT_MULT.get(s_unit, 1))
            received += int(float(r_num) * _UNIT_MULT.get(r_unit, 1))
        except ValueError:
            continue

    for match in _COST_RE.finditer(output or ""):
        try:
            cost += float(match.group(1))
        except ValueError:
            continue

    return {"tokens_sent": sent, "tokens_received": received, "cost_usd": cost}


def _empty_stats():
    return {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0}


def _add_stats(a, b):
    return {
        "tokens_sent": a["tokens_sent"] + b["tokens_sent"],
        "tokens_received": a["tokens_received"] + b["tokens_received"],
        "cost_usd": a["cost_usd"] + b["cost_usd"],
    }


def run_aider(model, message, target_file, read_files=None):
    """
    Run aider with a specific model, handling rate limit retries.

    ``read_files`` is an optional iterable of paths to attach as read-only
    context (spec, test, dependency target files, etc.). Missing paths are
    silently skipped so a single stale reference doesn't kill the run.

    Returns ``(success, stats)`` where ``stats`` is the dict produced by
    ``_parse_usage`` summed across every internal retry. Failed runs
    still return whatever stats accrued before the failure (a model can
    burn tokens before crashing).
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

    if read_files:
        from pathlib import Path as _Path
        seen = set()
        for rf in read_files:
            if not rf or rf == target_file or rf in seen:
                continue
            seen.add(rf)
            if _Path(rf).exists():
                cmd.extend(["--read", rf])

    # Disable LiteLLM's internal retry loop — we handle retries with correct wait times
    aider_env = os.environ.copy()
    aider_env["LITELLM_NUM_RETRIES"] = "0"

    invocation_stats = _empty_stats()
    fallback_wait = 15
    for attempt in range(1, AIDER_MAX_RATE_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=aider_env)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")

        combined = (result.stdout or "") + (result.stderr or "")
        invocation_stats = _add_stats(invocation_stats, _parse_usage(combined))

        if result.returncode == 0:
            return True, invocation_stats

        if "rate_limit_exceeded" in combined or "Rate limit reached" in combined:
            # "Request too large" means the request itself exceeds the TPM cap —
            # no amount of waiting will help. Fail immediately so the tier
            # fallback can try the next model.
            if "Request too large" in combined:
                print(f"  [request too large for model {model} TPM cap — falling back]")
                return False, invocation_stats

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
        return False, invocation_stats

    return False, invocation_stats


def run_with_tier_fallback(tier_name, message, target_file, start_model=None, read_files=None):
    """
    Try all models in a tier with fallback.

    Tries each model in the tier. On rate limit exhaustion for one model,
    moves to the next. Returns ``(success, model_used, stats)``. The
    ``stats`` dict aggregates token / cost usage across every model
    attempted in this call (including failed attempts that still spent
    tokens). ``read_files`` is forwarded to aider as read-only context.
    """
    tier = get_tier(tier_name)
    models = tier["models"]

    if start_model and start_model in models:
        idx = models.index(start_model)
        models = models[idx:]

    aggregate = _empty_stats()
    for model in models:
        print(f"  Trying {model}...")
        success, stats = run_aider(model, message, target_file, read_files=read_files)
        aggregate = _add_stats(aggregate, stats)
        if success:
            return True, model, aggregate

    return False, None, aggregate
