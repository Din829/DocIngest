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

from .utils.resources import resource_root


# ---------------------------------------------------------------------------
# Default config location (bundled with package)
# ---------------------------------------------------------------------------

# Project root in dev, sys._MEIPASS when frozen (PyInstaller). Same value as
# the old Path(__file__).parent.parent.parent in dev; only frozen differs, so
# the bundled default.yaml is found whether running from source or an exe.
_PROJECT_ROOT = resource_root()
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "default.yaml"


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """
    Raised by load_config / load_yaml when a user-facing config problem is
    detected: explicit -c path missing, YAML syntax error, top-level not a
    mapping, etc. CLI catches this and prints a friendly hint; library
    callers can catch it instead of yaml.YAMLError + FileNotFoundError +
    silent {} fallthrough.
    """
    pass


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
    """
    Load a YAML file as a mapping.

    Behaviour:
      - Path does not exist → returns {} (auto-discovery callers rely on this).
      - File exists but YAML is malformed → raises ConfigError with file +
        line/column hint.
      - File loads but top-level is not a mapping (a list, scalar, or null)
        → raises ConfigError. Callers expect dict-shaped config.
      - File loads as an empty document (`null` / whitespace) → returns {}
        (treated as "user wrote nothing", not a structural error).
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        # PyYAML attaches mark info on parser errors — surface it when
        # available so users see the exact line. Fall through to the bare
        # message otherwise.
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            location = f" at line {mark.line + 1}, column {mark.column + 1}"
        else:
            location = ""
        raise ConfigError(
            f"failed to parse config: {path}\n"
            f"  yaml error{location}: {getattr(e, 'problem', None) or e}\n"
            f"  Hint: check indentation / quoting around that spot. "
            f"See config/default.yaml for the expected shape."
        ) from e

    if data is None:
        # Empty file — treat as no overrides, not an error.
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"config file {path} top-level must be a mapping (key: value), "
            f"got {type(data).__name__}.\n"
            f"  Hint: see config/default.yaml for the expected shape."
        )
    return data


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
    # When the caller passed an explicit path, treat "missing" as a hard
    # error — silent fall-through used to let typos like `-c bad.yaml` look
    # like a successful run that ignored the user's overrides.
    # When auto-discovering (no path passed), missing is fine — the project
    # simply has no overrides.
    if project_config_path is not None:
        project_path = Path(project_config_path)
        if not project_path.exists():
            raise ConfigError(
                f"config file not found: {project_path}\n"
                f"  Hint: check the path, or omit -c / --config to "
                f"auto-discover docingest.yaml in the current directory."
            )
    else:
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


# ---------------------------------------------------------------------------
# User settings persistence (GUI settings screens) — user-level, cross-project
# ---------------------------------------------------------------------------
#
# load_config only READS layered config; the GUI's settings screens (model /
# cost limits) need to PERSIST user choices. These store a small user-level
# overrides file at ~/.docingest/config.yaml. They intentionally do NOT change
# load_config's layering — the bridge/adapter passes get_settings() into
# ingest as config_overrides, so the api default stays untouched (see
# BACKEND_API.md). Read what you wrote; malformed file fails loud via load_yaml.

_USER_SETTINGS_PATH = Path.home() / ".docingest" / "config.yaml"


def get_settings() -> dict[str, Any]:
    """Read the user-level settings overrides (~/.docingest/config.yaml).
    Returns {} when none saved yet."""
    if not _USER_SETTINGS_PATH.exists():
        return {}
    return load_yaml(_USER_SETTINGS_PATH) or {}


def save_settings(settings: dict[str, Any]) -> Path:
    """Persist the user-level settings overrides, replacing the file.
    Creates ~/.docingest/ if absent. Returns the written path."""
    _USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USER_SETTINGS_PATH.write_text(
        yaml.safe_dump(settings, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return _USER_SETTINGS_PATH
