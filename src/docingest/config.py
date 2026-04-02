"""
Configuration loading with layered merge.

Priority (highest wins):
  1. CLI arguments (runtime overrides)
  2. Project config (docingest.yaml in working directory)
  3. Default config (bundled default.yaml)

Design: Deep merge dictionaries so project config only needs to specify
overrides, not the entire config. All unspecified values fall back to defaults.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Default config location (bundled with package)
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent.parent  # src/docingest -> src -> DocIngest
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "default.yaml"


# ---------------------------------------------------------------------------
# Deep merge utility
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge `override` into `base`.

    - dict + dict → recursive merge
    - any other conflict → override wins
    - base keys not in override → preserved

    Returns a new dict (does not mutate inputs).
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file. Returns empty dict if file doesn't exist."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_config(
    project_config_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Load configuration with layered merge.

    Args:
        project_config_path: Path to project-specific docingest.yaml.
            If None, looks for docingest.yaml in current working directory.
        cli_overrides: Dictionary of CLI argument overrides (highest priority).

    Returns:
        Merged configuration dictionary.
    """
    # Layer 1: Default config (always loaded)
    config = load_yaml(_DEFAULT_CONFIG_PATH)
    if not config:
        raise FileNotFoundError(
            f"Default config not found at {_DEFAULT_CONFIG_PATH}. "
            "Package installation may be corrupted."
        )

    # Layer 2: Project config (optional)
    if project_config_path is not None:
        project_path = Path(project_config_path)
    else:
        # Auto-discover docingest.yaml in current working directory
        project_path = Path.cwd() / "docingest.yaml"

    if project_path.exists():
        project_config = load_yaml(project_path)
        if project_config:
            config = deep_merge(config, project_config)

    # Layer 3: CLI overrides (highest priority)
    if cli_overrides:
        config = deep_merge(config, cli_overrides)

    return config


# ---------------------------------------------------------------------------
# Config accessor helpers
# ---------------------------------------------------------------------------

def get_nested(config: dict, path: str, default: Any = None) -> Any:
    """
    Get a value from nested dict using dot-separated path.

    Example:
        get_nested(config, "chunking.max_tokens", 512)
        get_nested(config, "models.vision.primary.model")
    """
    keys = path.split(".")
    current = config
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current
