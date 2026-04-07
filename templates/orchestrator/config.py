"""Load models.yaml and expose config as a module-level dict."""

from pathlib import Path
import yaml

CONFIG_FILE = Path(__file__).parent.parent / "models.yaml"

_config = None


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


def get_tier(name):
    """Return a tier dict by name (e.g., 'primary', 'escalation')."""
    cfg = get_config()
    for tier in cfg["tiers"]:
        if tier["name"] == name:
            return tier
    raise ValueError(f"Unknown tier: {name}")
