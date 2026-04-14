"""Model selection, fallback routing, and rate limit handling."""

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .config import get_config, get_tier
from .log import get_logger

log = get_logger("model_router")

# Max retries within a single aider invocation for rate limit errors
AIDER_MAX_RATE_RETRIES = 4
# Default timeout for a single aider invocation (seconds).
# Overridden by ``aider_timeout_seconds`` in models.yaml.
_AIDER_TIMEOUT_DEFAULT = 300

# ---------------------------------------------------------------------------
# Per-model rate-limit coordinator
#
# When a task hits a Groq 429 with a "try again in Ns" hint, the orchestrator
# records a per-model "earliest safe request time". Subsequent tasks (and
# subsequent retries within the same task) check this before each aider
# invocation and sleep the remaining window instead of wasting the call.
#
# Keyed by full model name ("groq/qwen/qwen3-32b") — Groq rate limits are
# per-model, not per-account, so hitting qwen3's window tells us nothing
# about kimi's. Foreign providers (gemini/*, etc.) slot in automatically.
#
# The dict is process-local and not persisted — rate-limit windows are
# seconds, shorter than typical between-run gaps.
# ---------------------------------------------------------------------------
_next_available_at: dict = {}
# Daily-quota-exhausted models: process-level, never clears within a run.
# Keyed by full model name. Different from _next_available_at (which expires
# after a timed window) and _request_too_large_tls (which is per-task/thread).
# When a Gemini RESOURCE_EXHAUSTED quota error is seen, the model is added here
# and all callers skip it immediately without sleeping. Not per-task because
# "daily quota" means the API key has no budget left regardless of which task
# is asking. Protected by the same _rate_limit_lock.
_daily_quota_exhausted: set = set()
_invalid_models: set = set()
_last_provider_pressure_at = 0.0
# Request-too-large is per-task and per-thread, not session-wide. A long spec
# that blows past qwen3's 6k TPM cap must not cause the NEXT task's tiny spec
# to also skip qwen3. We store the flag on a threading.local so worktree-mode
# parallel tasks don't poison each other either. Access still goes through
# _rate_limit_lock for consistency with the other coordinator state.
_request_too_large_tls = threading.local()
_rate_limit_lock = threading.Lock()

# Rough aider overhead beyond attached files: system prompt, repo map,
# orchestration prompt text, and retry/test-output context.
_AIDER_OVERHEAD_TOKENS = 2000
_PRE_SCREEN_SAFETY_MULTIPLIER = 1.4


def _request_too_large_set():
    """Return (creating if needed) the per-thread request-too-large set."""
    s = getattr(_request_too_large_tls, "models", None)
    if s is None:
        s = set()
        _request_too_large_tls.models = s
    return s

def clear_request_too_large():
    """Forget every request-too-large flag for the current task.

    Called at the top of ``run_task`` so the next task starts with a fresh
    model roster regardless of what the previous task learned.
    """
    with _rate_limit_lock:
        _request_too_large_set().clear()

# Indirected so tests can freeze time without monkeypatching stdlib
_clock = time.time
_sleep = time.sleep


def _wait_for_model(model):
    """Sleep until the cached rate-limit window for ``model`` has reset.

    No-ops when the model has no recorded window or the window has passed.
    Thread-safe: reads the window under a lock, then sleeps outside it.
    """
    with _rate_limit_lock:
        earliest = _next_available_at.get(model, 0.0)
    now = _clock()
    if earliest > now:
        wait = earliest - now
        log.info("  [coordinator: %s rate-limited, sleeping %.1fs before request]", model, wait)
        _sleep(wait)


def _mark_request_too_large(model):
    """Record that this model cannot handle the current request size.

    "Request too large" is permanent for *this task*: no amount of waiting
    fixes a TPM-cap exceeded error, so subsequent retry attempts within
    the same task skip the model. The flag clears at the start of the
    next task (see ``clear_request_too_large``) since a different task's
    spec may fit under the cap.
    Thread-safe.
    """
    with _rate_limit_lock:
        _request_too_large_set().add(model)

def is_request_too_large(model):
    """Return True if this model is flagged as too-large for the current task."""
    with _rate_limit_lock:
        return model in _request_too_large_set()


def mark_daily_quota_exhausted(model: str) -> None:
    """Record that ``model`` has exhausted its daily API quota for this process lifetime.

    Unlike per-minute rate limits, daily quota exhaustion cannot be resolved by
    sleeping. Callers skip the model immediately without waiting. Thread-safe.
    """
    with _rate_limit_lock:
        _daily_quota_exhausted.add(model)


def is_daily_quota_exhausted(model: str) -> bool:
    """Return True if this model's daily quota is known to be exhausted."""
    with _rate_limit_lock:
        return model in _daily_quota_exhausted


def mark_invalid_model(model: str) -> None:
    """Record that ``model`` is misconfigured or unsupported for this run."""
    with _rate_limit_lock:
        _invalid_models.add(model)


def is_invalid_model(model: str) -> bool:
    """Return True if the provider reported ``model`` as unsupported/not found."""
    with _rate_limit_lock:
        return model in _invalid_models


def all_gemini_quota_exhausted() -> bool:
    """Return True if every model in the gemini tier has exhausted its daily quota.

    Returns False when the gemini tier is not configured, so callers can
    unconditionally call this without guarding against missing config.
    """
    try:
        tier = get_tier("gemini")
    except ValueError:
        return False
    return all(is_daily_quota_exhausted(m) for m in tier["models"])


def has_pending_rate_limits() -> bool:
    """Return True when any model still has a future retry window recorded."""
    now = _clock()
    with _rate_limit_lock:
        return any(earliest > now for earliest in _next_available_at.values())


def adaptive_cooldown_seconds(max_cooldown):
    """Return a bounded cooldown based on recent provider-pressure signals."""
    if max_cooldown <= 0:
        return 0.0

    now = _clock()
    with _rate_limit_lock:
        pending_waits = [earliest - now for earliest in _next_available_at.values() if earliest > now]
        recent_pressure_age = now - _last_provider_pressure_at if _last_provider_pressure_at else None

    if pending_waits:
        return round(min(max_cooldown, max(pending_waits)), 2)
    if recent_pressure_age is not None and recent_pressure_age < max_cooldown:
        return round(max_cooldown - recent_pressure_age, 2)
    return 0.0


def _mark_rate_limited(model, retry_after, buffer=5.0):
    """Record that ``model`` should not be called for ``retry_after`` seconds.

    ``buffer`` adds headroom for Groq's sliding window reset imprecision and
    matches the buffer already applied inside the local retry loop.
    Thread-safe: writes under a lock.
    """
    global _last_provider_pressure_at
    with _rate_limit_lock:
        _next_available_at[model] = _clock() + retry_after + buffer
        _last_provider_pressure_at = _clock()


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


# Strings that indicate Gemini daily quota exhaustion (not a per-minute window).
# LiteLLM surfaces the raw gRPC RESOURCE_EXHAUSTED message from the Gemini API.
_GEMINI_DAILY_QUOTA_STRINGS = (
    "RESOURCE_EXHAUSTED",
    "quota exceeded",
    "daily quota",
    "Quota exceeded",
)


def _is_daily_quota_error(combined_output: str, retry_after) -> bool:
    """Return True if output signals a Gemini daily quota exhaustion.

    Distinguishes daily quota from per-minute rate limits: both use the
    RESOURCE_EXHAUSTED gRPC code, but per-minute 429s always include a
    "try again in Ns" retry-after field while daily quota errors do not.

    ``retry_after`` is the pre-computed result of ``_parse_retry_after(combined_output)``
    — the caller computes it once to avoid scanning the string twice.
    """
    return (
        any(s in combined_output for s in _GEMINI_DAILY_QUOTA_STRINGS)
        and retry_after is None
    )


_INVALID_MODEL_STRINGS = (
    '"status": "NOT_FOUND"',
    "is not found for API version",
    "litellm.NotFoundError",
    "does not exist for provider",
)


def _is_invalid_model_error(combined_output: str) -> bool:
    """Return True if provider output says the requested model id is invalid."""
    return any(s in combined_output for s in _INVALID_MODEL_STRINGS)


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
    return {"tokens_sent": 0, "tokens_received": 0, "cost_usd": 0.0, "wall_seconds": 0.0}


def _add_stats(a, b):
    return {
        "tokens_sent": a["tokens_sent"] + b["tokens_sent"],
        "tokens_received": a["tokens_received"] + b["tokens_received"],
        "cost_usd": a["cost_usd"] + b["cost_usd"],
        "wall_seconds": a.get("wall_seconds", 0.0) + b.get("wall_seconds", 0.0),
    }


def _estimate_request_tokens(target_file, message, read_files=None, cwd=None):
    """Estimate aider request tokens using file size, prompt size, and fixed overhead."""
    total_bytes = 0
    for path in [target_file] + list(read_files or []):
        resolved = Path(cwd) / path if cwd and not Path(path).is_absolute() else Path(path)
        try:
            total_bytes += resolved.stat().st_size
        except OSError:
            pass

    file_tokens = total_bytes // 4
    message_tokens = len(message) // 4 if message else 0
    raw_estimate = file_tokens + message_tokens + _AIDER_OVERHEAD_TOKENS
    return int(raw_estimate * _PRE_SCREEN_SAFETY_MULTIPLIER)


def run_aider(model, message, target_file, read_files=None, cwd=None, timeout_override=None):
    """
    Run aider with a specific model, handling rate limit retries.

    ``read_files`` is an optional iterable of paths to attach as read-only
    context (spec, test, dependency target files, etc.). Missing paths are
    silently skipped so a single stale reference doesn't kill the run.

    When ``cwd`` is set, aider runs inside that directory (e.g. a git
    worktree) and all relative paths resolve there.

    Returns ``(success, stats, reason)`` where ``stats`` is the dict produced by
    ``_parse_usage`` summed across every internal retry. Failed runs
    still return whatever stats accrued before the failure (a model can
    burn tokens before crashing).
    """
    cfg = get_config()
    weak_model = cfg.get("weak_model", "groq/llama-3.1-8b-instant")
    aider_timeout = (
        timeout_override
        if timeout_override is not None
        else int(cfg.get("aider_timeout_seconds", _AIDER_TIMEOUT_DEFAULT))
    )

    # Resolve the target file to an absolute path and ensure its parent
    # directory exists. If aider is handed a relative path to a file whose
    # parent directory does not exist yet, it prints "Git repo: none" and
    # silently falls back to cwd-relative path creation, which has been
    # observed to produce spurious nested `src/` directories and duplicate
    # class errors. Creating the parent up-front and passing an absolute
    # path keeps aider anchored to the real worktree.
    target_path = Path(target_file)
    if cwd and not target_path.is_absolute():
        target_path = Path(cwd) / target_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_file_abs = str(target_path)

    cmd = [
        "aider",
        "--model", model,
        "--message", message,
        "--file", target_file_abs,
        "--weak-model", weak_model,
        "--yes-always",
        "--auto-commits",
        "--no-stream",
        "--no-show-model-warnings",
        "--no-auto-lint",
    ]

    if read_files:
        seen = set()
        for rf in read_files:
            if not rf or rf == target_file or rf in seen:
                continue
            seen.add(rf)
            resolved = Path(cwd) / rf if cwd and not Path(rf).is_absolute() else Path(rf)
            if resolved.exists():
                cmd.extend(["--read", str(resolved)])

    # Disable LiteLLM's internal retry loop — we handle retries with correct wait times
    aider_env = os.environ.copy()
    aider_env["LITELLM_NUM_RETRIES"] = "0"

    invocation_stats = _empty_stats()
    t0 = time.time()

    def _finish(success, reason):
        invocation_stats["wall_seconds"] = round(time.time() - t0, 2)
        return success, invocation_stats, reason

    fallback_wait = 15
    for attempt in range(1, AIDER_MAX_RATE_RETRIES + 1):
        # Respect any rate-limit window recorded for this model by a
        # previous task or a previous attempt in this task.
        _wait_for_model(model)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                env=aider_env, timeout=aider_timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            log.warning("  [aider timed out after %ds on model %s]", aider_timeout, model)
            return _finish(False, "timeout")
        if result.stdout:
            log.info("%s", result.stdout.rstrip())
        if result.stderr:
            # Filter GIO URL-open noise: aider prints billing URLs and the OS
            # tries to open them as GIO URIs, producing spurious "Operation not
            # supported" lines that clutter the debug log.
            stderr_clean = "\n".join(
                line for line in (result.stderr or "").splitlines()
                if not line.startswith("gio:")
            )
            if stderr_clean:
                log.debug("%s", stderr_clean.rstrip())

        combined = (result.stdout or "") + (result.stderr or "")
        invocation_stats = _add_stats(invocation_stats, _parse_usage(combined))

        # Check for rate-limit / too-large errors BEFORE trusting the exit code.
        # Aider exits 0 when it exhausts its own retries on "Request too large"
        # (it creates/touches the file but writes nothing). If we blindly trust
        # returncode == 0 here, run_with_tier_fallback never tries the next model.
        #
        # Compute retry_after once — reused by both the daily-quota check and
        # the per-minute rate-limit handler to avoid scanning combined twice.
        retry_after = _parse_retry_after(combined)

        # Daily quota exhaustion: quota string present AND no retry-after hint.
        # No amount of sleeping fixes this — skip the model immediately.
        if _is_daily_quota_error(combined, retry_after):
            log.warning("  [Gemini daily quota exhausted for %s -- skipping model]", model)
            mark_daily_quota_exhausted(model)
            return _finish(False, "daily_quota_exhausted")

        if _is_invalid_model_error(combined):
            log.warning("  [invalid model config for %s -- skipping model]", model)
            mark_invalid_model(model)
            return _finish(False, "invalid_model_config")

        if "rate_limit_exceeded" in combined or "Rate limit reached" in combined:
            # "Request too large" means the request itself exceeds the TPM cap —
            # no amount of waiting will help, and it is not a time-based window
            # so we do not record it in the coordinator.
            if "Request too large" in combined:
                log.warning("  [request too large for model %s TPM cap -- falling back]", model)
                _mark_request_too_large(model)
                return _finish(False, "request_too_large")

            wait = retry_after
            if wait:
                _mark_rate_limited(model, wait)  # inform future tasks/attempts
                wait += 5  # buffer for sliding window reset
                log.info("  [rate limit: sleeping %.1fs as instructed by Groq (attempt %d/%d)]", wait, attempt, AIDER_MAX_RATE_RETRIES)
            else:
                _mark_rate_limited(model, fallback_wait)  # best-effort record
                wait = fallback_wait
                log.info("  [rate limit: sleeping %ds (exponential backoff, attempt %d/%d)]", wait, attempt, AIDER_MAX_RATE_RETRIES)
                fallback_wait = min(fallback_wait * 2, 120)

            if attempt < AIDER_MAX_RATE_RETRIES:
                _sleep(wait)
                continue
            return _finish(False, "rate_limit")

        # No rate-limit error — trust the exit code.
        if result.returncode == 0:
            return _finish(True, "ok")

        # Non-rate-limit error or exhausted retries
        return _finish(False, "error")

    return _finish(False, "rate_limit")


def run_with_tier_fallback(tier_name, message, target_file, start_model=None, read_files=None,
                           cwd=None):
    """
    Try all models in a tier with fallback.

    Tries each model in the tier. On rate limit exhaustion for one model,
    moves to the next. Returns ``(success, model_used, stats, attempts)``. The
    ``stats`` dict aggregates token / cost usage across every model
    attempted in this call (including failed attempts that still spent
    tokens). ``read_files`` is forwarded to aider as read-only context.
    """
    tier = get_tier(tier_name)
    models = tier["models"]
    model_meta = tier.get("model_meta", {})
    tier_timeout = tier.get("aider_timeout_seconds")

    if start_model and start_model in models:
        idx = models.index(start_model)
        models = models[idx:]

    aggregate = _empty_stats()
    attempts = []
    estimated_tokens = _estimate_request_tokens(target_file, message, read_files, cwd)
    for model in models:
        meta = model_meta.get(model, {})
        cap = meta.get("max_input_tokens")
        if cap and estimated_tokens > cap:
            log.info(
                "  Skipping %s -- estimated %d tokens exceeds declared cap %d",
                model,
                estimated_tokens,
                cap,
            )
            attempts.append(
                {"model": model, "reason": "pre_screen_too_large", "success": False, "wall_seconds": 0.0}
            )
            continue
        if is_request_too_large(model):
            log.info("  Skipping %s -- request too large (already seen this task)", model)
            attempts.append(
                {"model": model, "reason": "request_too_large", "success": False, "wall_seconds": 0.0}
            )
            continue
        if is_daily_quota_exhausted(model):
            log.info("  Skipping %s -- daily quota exhausted", model)
            attempts.append(
                {"model": model, "reason": "daily_quota_exhausted", "success": False, "wall_seconds": 0.0}
            )
            continue
        if is_invalid_model(model):
            log.info("  Skipping %s -- invalid model config", model)
            attempts.append(
                {"model": model, "reason": "invalid_model_config", "success": False, "wall_seconds": 0.0}
            )
            continue
        log.info("  Trying %s...", model)
        t0 = time.time()
        success, stats, reason = run_aider(
            model,
            message,
            target_file,
            read_files=read_files,
            cwd=cwd,
            timeout_override=tier_timeout,
        )
        model_wall = round(time.time() - t0, 2)
        aggregate = _add_stats(aggregate, stats)
        attempts.append(
            {"model": model, "reason": reason, "success": success, "wall_seconds": model_wall}
        )
        if success:
            return True, model, aggregate, attempts

    return False, None, aggregate, attempts
