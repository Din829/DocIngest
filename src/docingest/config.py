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
import os
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


# ---------------------------------------------------------------------------
# Environment variable override
# ---------------------------------------------------------------------------

# Prefix for all config-overriding environment variables.
# Use double underscore (`__`) as the nesting separator so that single-
# underscore key names like `max_tokens` remain intact.
#
# Examples:
#   DOCINGEST__chunking__max_tokens=1024
#   DOCINGEST__parsing__docx__max_page_images=50
#   DOCINGEST__models__vision__primary__model=gemini-3-pro-preview
#   DOCINGEST__incremental__enabled=false
_ENV_PREFIX = "DOCINGEST__"


def _parse_env_value(raw: str) -> Any:
    """
    Convert an environment variable string to a best-effort Python value.

    Supports: bool (true/false/yes/no), int, float, null, and plain string.
    Matches common YAML scalar conventions.
    """
    stripped = raw.strip()
    lower = stripped.lower()

    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    if lower in ("null", "none", "~", ""):
        return None

    # Integer (including negative)
    if stripped.lstrip("-+").isdigit():
        try:
            return int(stripped)
        except ValueError:
            pass

    # Float (has decimal point or scientific notation)
    if "." in stripped or "e" in lower:
        try:
            return float(stripped)
        except ValueError:
            pass

    # Default: plain string
    return stripped


def _set_nested(target: dict, path: list[str], value: Any) -> None:
    """Set a nested dict value from a dotted path, creating intermediate dicts."""
    current = target
    for key in path[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[path[-1]] = value


def load_env_overrides() -> dict[str, Any]:
    """
    Build an override dict from environment variables matching DOCINGEST__*.

    Key names are case-insensitive (Windows uppercases all env vars), so
    keys are normalized to lowercase to match the YAML config convention.
    Values are parsed with best-effort type inference.

    Example:
        DOCINGEST__chunking__max_tokens=1024
        → {"chunking": {"max_tokens": 1024}}

    Model-name-style values (e.g. "gemini-3-pro-preview", "GPT-4") are
    preserved in their original case — only the config path keys are lowered.
    """
    overrides: dict[str, Any] = {}
    prefix_upper = _ENV_PREFIX.upper()
    for raw_key, raw_value in os.environ.items():
        # Prefix check is case-insensitive for Windows compatibility
        if not raw_key.upper().startswith(prefix_upper):
            continue
        path_str = raw_key[len(_ENV_PREFIX):]
        if not path_str:
            continue
        # Normalize path keys to lowercase (config convention)
        # VALUE is NOT touched — model names etc. keep their original case
        path = [seg.lower() for seg in path_str.split("__") if seg]
        if not path:
            continue
        value = _parse_env_value(raw_value)
        _set_nested(overrides, path, value)
    return overrides


def load_config(
    project_config_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Load configuration with layered merge.

    Priority (highest wins):
      1. CLI arguments (runtime overrides)
      2. Environment variables (DOCINGEST__* prefix)
      3. Project config (docingest.yaml in working directory)
      4. Default config (bundled default.yaml)

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

    # Layer 3: Environment variables (DOCINGEST__*)
    env_overrides = load_env_overrides()
    if env_overrides:
        config = deep_merge(config, env_overrides)

    # Layer 4: CLI overrides (highest priority)
    if cli_overrides:
        config = deep_merge(config, cli_overrides)

    _inject_model_defaults(config)
    return config


def _inject_model_defaults(config: dict) -> None:
    """
    Inject `models.defaults` into every sibling task dict as `_defaults`.

    After this, any `models.<task>` dict (vision, chunking_assist, ...) can be
    passed to an LLM helper and resolve_max_tokens() will fall through to
    models.defaults.max_response_tokens when the task has no explicit override.
    This means future tasks inherit the global cap with zero new code —
    the only place a token cap is defined is config/default.yaml.

    Safe to call multiple times (idempotent). Does nothing if models.defaults
    is missing (respects user configs that strip it out).
    """
    models = config.get("models")
    if not isinstance(models, dict):
        return
    defaults = models.get("defaults")
    if not isinstance(defaults, dict):
        return
    for task_name, task_cfg in models.items():
        if task_name == "defaults" or not isinstance(task_cfg, dict):
            continue
        # Overwrite rather than merge — _defaults must reflect the current
        # resolved global defaults, not a stale copy from a previous load.
        task_cfg["_defaults"] = defaults


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
