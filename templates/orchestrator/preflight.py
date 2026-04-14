"""Startup and test-runtime preflight checks."""

import os
import shutil
import subprocess
import threading
from pathlib import Path

from .config import get_config, model_id as _model_id
from .log import get_logger

log = get_logger("preflight")

_KNOWN_BAD_MODEL_IDS = {
    # Placeholder kept in older templates; observed to return provider 404s.
    "gemini/gemini-3.0-flash": (
        "Invalid placeholder model id. Replace it with a supported Gemini model "
        "before running the pipeline."
    ),
}
_WARMED_MAVEN_ROOTS: set[str] = set()
_MAVEN_WARMUP_LOCK = threading.Lock()
MAVEN_WARMUP_TIMEOUT = 300


def normalize_provider_env() -> list[str]:
    """Normalize provider env vars and return any informational warnings."""
    warnings = []
    google = os.environ.get("GOOGLE_API_KEY")
    gemini = os.environ.get("GEMINI_API_KEY")

    if gemini and not google:
        os.environ["GOOGLE_API_KEY"] = gemini
        warnings.append(
            "Using legacy GEMINI_API_KEY env var; mirrored it to GOOGLE_API_KEY for provider compatibility."
        )
    elif google and not gemini:
        # Keep legacy docs/users working if local scripts still look for GEMINI_API_KEY.
        os.environ["GEMINI_API_KEY"] = google

    return warnings


def validate_config(cfg=None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for the loaded models.yaml config."""
    cfg = cfg or get_config()
    errors: list[str] = []
    warnings: list[str] = []

    tiers = cfg.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return ["models.yaml must define a non-empty 'tiers' list."], warnings

    tier_names = set()
    seen_models = {}
    for idx, tier in enumerate(tiers, 1):
        name = tier.get("name", "").strip()
        retries = tier.get("retries")
        tier_timeout = tier.get("aider_timeout_seconds")
        models = tier.get("models") or []

        if not name:
            errors.append(f"Tier #{idx} is missing a name.")
            continue
        if name in tier_names:
            errors.append(f"Duplicate tier name '{name}' in models.yaml.")
        tier_names.add(name)

        if not isinstance(retries, int) or retries < 1:
            errors.append(f"Tier '{name}' must set retries to an integer >= 1.")
        if tier_timeout is not None and (
            not isinstance(tier_timeout, (int, float)) or tier_timeout < 1
        ):
            errors.append(
                f"Tier '{name}': aider_timeout_seconds must be a positive number when set."
            )

        if not isinstance(models, list) or not models:
            errors.append(f"Tier '{name}' must declare at least one model.")
            continue

        for model in models:
            model_id = model if isinstance(model, str) else model.get("id", "") if isinstance(model, dict) else ""
            if not isinstance(model_id, str) or "/" not in model_id:
                errors.append(f"Tier '{name}' has invalid model id: {model!r}")
                continue
            if isinstance(model, dict):
                cap = model.get("max_input_tokens")
                if cap is not None and (not isinstance(cap, int) or cap < 1):
                    errors.append(
                        f"Tier '{name}' model '{model_id}': max_input_tokens must be a positive integer."
                    )

            if any(marker in model_id for marker in ("<", ">", "your-", "placeholder")):
                errors.append(f"Tier '{name}' contains placeholder model id '{model_id}'.")
            bad_reason = _KNOWN_BAD_MODEL_IDS.get(model_id)
            if bad_reason:
                errors.append(f"Tier '{name}' model '{model_id}': {bad_reason}")

            previous_tier = seen_models.setdefault(model_id, name)
            if previous_tier != name:
                warnings.append(
                    f"Model '{model_id}' appears in both tiers '{previous_tier}' and '{name}'. "
                    "That usually wastes retries instead of giving a true fallback."
                )

    return errors, warnings


def validate_provider_env(cfg=None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for provider env vars required by configured models."""
    cfg = cfg or get_config()
    warnings = normalize_provider_env()
    errors: list[str] = []

    required_envs = set()
    for tier in cfg.get("tiers", []):
        for raw_model in tier.get("models") or []:
            mid = _model_id(raw_model)
            if mid.startswith("groq/"):
                required_envs.add("GROQ_API_KEY")
            elif mid.startswith("gemini/"):
                required_envs.add("GOOGLE_API_KEY")

    for env_name in sorted(required_envs):
        if not os.environ.get(env_name):
            if env_name == "GOOGLE_API_KEY":
                errors.append(
                    "Missing GOOGLE_API_KEY for configured Gemini models. "
                    "GEMINI_API_KEY is also accepted and will be mirrored automatically."
                )
            else:
                errors.append(f"Missing {env_name} for configured models.")

    return errors, warnings


def validate_runtime_prereqs(repo_root=None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for runtime/tool readiness.

    This validates executables required by the configured/generated workflow and
    eagerly warms the Maven dependency cache when a Java project is detected so
    offline task tests do not fail later for avoidable environment reasons.
    """
    errors: list[str] = []
    warnings: list[str] = []
    root = Path(repo_root or Path.cwd()).resolve()

    if shutil.which("aider") is None:
        errors.append("Missing 'aider' on PATH. Install Aider before running the pipeline.")

    pom = root / "pom.xml"
    if pom.exists():
        if shutil.which("mvn") is None:
            errors.append("Found pom.xml but 'mvn' is not on PATH.")
        else:
            warmed, output = maybe_prime_maven_cache(root, reason="startup preflight")
            if not warmed:
                detail = output.strip().splitlines()[-1] if output.strip() else "unknown error"
                errors.append(
                    "Maven dependency warmup failed during preflight. "
                    f"Offline task tests will not be reliable until this is fixed. Last line: {detail}"
                )

    return errors, warnings


def run_startup_preflight(repo_root=None) -> tuple[list[str], list[str]]:
    """Run config, environment, and runtime validation."""
    cfg = get_config()
    config_errors, config_warnings = validate_config(cfg)
    env_errors, env_warnings = validate_provider_env(cfg)
    runtime_errors, runtime_warnings = validate_runtime_prereqs(repo_root=repo_root)
    return (
        config_errors + env_errors + runtime_errors,
        config_warnings + env_warnings + runtime_warnings,
    )


def maybe_prime_maven_cache(repo_root, reason=None) -> tuple[bool, str]:
    """Warm the Maven dependency cache once per repo root when a pom.xml exists."""
    root = Path(repo_root).resolve()
    pom = root / "pom.xml"
    if not pom.exists():
        return False, ""

    cache_key = str(root)
    with _MAVEN_WARMUP_LOCK:
        if cache_key in _WARMED_MAVEN_ROOTS:
            return True, ""

        cmd = ["mvn", "-q", "-DskipTests", "dependency:go-offline"]
        if reason:
            log.info("Preflight: warming Maven cache (%s)", reason)
        else:
            log.info("Preflight: warming Maven cache")

        result = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=MAVEN_WARMUP_TIMEOUT,
            check=False,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            _WARMED_MAVEN_ROOTS.add(cache_key)
            return True, combined

        return False, combined
