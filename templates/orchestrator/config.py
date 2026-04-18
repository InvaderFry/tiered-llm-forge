"""Load models.yaml and expose config as a module-level dict."""

from pathlib import Path

import yaml

CONFIG_FILE = Path(__file__).parent.parent / "models.yaml"

_config = None


def normalize_model_entry(entry):
    """Normalize a model entry to a dict with an ``id`` and optional metadata."""
    if isinstance(entry, str):
        return {"id": entry, "max_input_tokens": None}
    if isinstance(entry, dict):
        return {
            "id": entry["id"],
            "max_input_tokens": entry.get("max_input_tokens"),
        }
    raise ValueError(f"Invalid model entry: {entry!r}")


def model_id(entry):
    """Extract the model-id string from either a string or dict config entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("id", "")
    return ""


def load_config(path=None):
    """Load and cache the models.yaml configuration."""
    global _config
    config_path = Path(path) if path else CONFIG_FILE
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Run from the project root where models.yaml lives."
        )
    with open(config_path) as f:
        _config = yaml.safe_load(f)
    return _config


def get_config():
    """Return cached config, loading from default path if needed."""
    if _config is None:
        return load_config()
    return _config


def get_auto_parallel():
    """Return whether auto-parallel mode is enabled in the loaded config."""
    return bool(get_config().get("auto_parallel", False))


def get_tier(name):
    """Return a tier dict by name with normalized model metadata."""
    cfg = get_config()
    for tier in cfg["tiers"]:
        if tier["name"] == name:
            raw_models = tier.get("models", [])
            normalized = [normalize_model_entry(m) for m in raw_models]
            return {
                **tier,
                "models": [m["id"] for m in normalized],
                "model_meta": {m["id"]: m for m in normalized},
            }
    raise ValueError(f"Unknown tier: {name}")
