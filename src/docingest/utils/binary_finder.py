"""
Generic external-binary locator.

Problem
-------
`shutil.which()` only searches PATH. On Windows most GUI tools (LibreOffice,
ExifTool, ffmpeg, …) install into Program Files and never touch PATH, so
DocIngest would silently fall back to degraded paths on a perfectly
normal developer machine. On macOS the /Applications paths are equally
invisible to `which`. The fix is a small lookup that knows where typical
installers drop their binaries per platform.

Design
------
The lookup chain for a given binary name, first hit wins:

  1. Explicit config override (e.g. `binaries.soffice.path` in YAML or
     `DOCINGEST__binaries__soffice__path` env var). Highest priority so
     users / CI can pin exact binaries.
  2. Dedicated environment variables (e.g. `SOFFICE_PATH`). These are
     the conventional way operators configure tools in Docker / CI.
  3. `shutil.which()` — the Linux / proper-install case.
  4. Platform-specific known install paths (data driven, see
     `_KNOWN_BINARIES`).

Adding a new binary: extend `_KNOWN_BINARIES`. That's it. The lookup
chain is applied uniformly, so no new code paths per binary.

Non-goals
---------
* Not a package manager. We don't install missing binaries.
* Not a version checker. Callers handle version requirements (see
  `hooks/file_metadata.py::_check_exiftool_version` for the exiftool CVE
  check). This module only answers "where is it?".
* No disk caching. The filesystem is fast enough; stale paths would be
  worse than a 1ms per-run discovery pass.

Usage
-----
    from docingest.utils.binary_finder import find_binary

    soffice = find_binary("soffice", config)
    if soffice:
        subprocess.run([soffice, "--headless", ...])
    else:
        logger.info("LibreOffice not found; skipping PDF rendering")
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

from ..config import get_nested


# ---------------------------------------------------------------------------
# Known binary catalogue
# ---------------------------------------------------------------------------
#
# Per binary we track:
#   - canonical name   (the key used by callers, also the shutil.which() name)
#   - aliases          (alternative lookup names, e.g. "libreoffice" → "soffice")
#   - env_vars         (conventional environment variables to check)
#   - paths            (per-platform fallback absolute-path candidates)
#
# Path candidates can include environment variables (${ProgramFiles}) — they
# are expanded at lookup time so users with custom install drives still hit.

_KNOWN_BINARIES: dict[str, dict[str, Any]] = {
    "soffice": {
        "aliases": ["libreoffice"],
        "env_vars": ["SOFFICE_PATH", "LIBREOFFICE_PATH"],
        "paths": {
            "win32": [
                r"${ProgramFiles}\LibreOffice\program\soffice.exe",
                r"${ProgramFiles(x86)}\LibreOffice\program\soffice.exe",
                r"${ProgramW6432}\LibreOffice\program\soffice.exe",
            ],
            "darwin": [
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                "/opt/homebrew/bin/soffice",
                "/usr/local/bin/soffice",
            ],
            "linux": [
                "/usr/bin/soffice",
                "/usr/local/bin/soffice",
                "/snap/bin/libreoffice",
                "/opt/libreoffice/program/soffice",
            ],
        },
    },
    "exiftool": {
        "aliases": [],
        "env_vars": ["EXIFTOOL_PATH"],
        "paths": {
            "win32": [
                r"${ProgramFiles}\exiftool\exiftool.exe",
                r"${ProgramFiles(x86)}\exiftool\exiftool.exe",
                r"${ProgramFiles}\ExifTool\exiftool.exe",
                r"${LOCALAPPDATA}\Programs\exiftool\exiftool.exe",
            ],
            "darwin": [
                "/opt/homebrew/bin/exiftool",
                "/usr/local/bin/exiftool",
                "/opt/local/bin/exiftool",
            ],
            "linux": [
                "/usr/bin/exiftool",
                "/usr/local/bin/exiftool",
            ],
        },
    },
    "ffmpeg": {
        "aliases": [],
        "env_vars": ["FFMPEG_PATH"],
        "paths": {
            "win32": [
                r"${ProgramFiles}\ffmpeg\bin\ffmpeg.exe",
                r"${ProgramFiles}\ffmpeg-7.1.1-full_build\bin\ffmpeg.exe",
                r"${LOCALAPPDATA}\Programs\ffmpeg\bin\ffmpeg.exe",
                r"C:\ffmpeg\bin\ffmpeg.exe",
            ],
            "darwin": [
                "/opt/homebrew/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
            ],
            "linux": [
                "/usr/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
            ],
        },
    },
    "ffprobe": {
        "aliases": [],
        "env_vars": ["FFPROBE_PATH"],
        "paths": {
            "win32": [
                r"${ProgramFiles}\ffmpeg\bin\ffprobe.exe",
                r"${ProgramFiles}\ffmpeg-7.1.1-full_build\bin\ffprobe.exe",
                r"${LOCALAPPDATA}\Programs\ffmpeg\bin\ffprobe.exe",
                r"C:\ffmpeg\bin\ffprobe.exe",
            ],
            "darwin": [
                "/opt/homebrew/bin/ffprobe",
                "/usr/local/bin/ffprobe",
            ],
            "linux": [
                "/usr/bin/ffprobe",
                "/usr/local/bin/ffprobe",
            ],
        },
    },
    "yt-dlp": {
        "aliases": ["yt_dlp", "youtube-dl"],
        "env_vars": ["YT_DLP_PATH"],
        "paths": {
            "win32": [
                r"${ProgramFiles}\yt-dlp\yt-dlp.exe",
                r"${LOCALAPPDATA}\Programs\yt-dlp\yt-dlp.exe",
            ],
            "darwin": [
                "/opt/homebrew/bin/yt-dlp",
                "/usr/local/bin/yt-dlp",
            ],
            "linux": [
                "/usr/bin/yt-dlp",
                "/usr/local/bin/yt-dlp",
            ],
        },
    },
}


def _resolve_alias(name: str) -> str:
    """Map an alias (e.g. 'libreoffice') back to its canonical entry key."""
    lname = name.lower()
    if lname in _KNOWN_BINARIES:
        return lname
    for canonical, entry in _KNOWN_BINARIES.items():
        if lname in entry.get("aliases", []):
            return canonical
    return lname  # unknown — returned as-is so callers still get which() support


def _platform_key() -> str:
    """Return the sys.platform key used in _KNOWN_BINARIES."""
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _expand(candidate: str) -> str:
    """Expand ${VAR} style env vars in a path candidate. Missing vars stay literal."""
    return os.path.expandvars(os.path.expanduser(candidate))


def _first_existing(candidates: Iterable[str]) -> str | None:
    """Return the first path from candidates that points at a real file."""
    for candidate in candidates:
        expanded = _expand(candidate)
        # If expansion left a literal ${VAR} because the env var is unset,
        # skip it — Path.exists() would return False anyway but explicit
        # is better than silent.
        if "${" in expanded:
            continue
        p = Path(expanded)
        if p.exists() and p.is_file():
            return str(p)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_binary(
    name: str,
    config: dict[str, Any] | None = None,
) -> str | None:
    """
    Locate an external binary by name, trying multiple discovery strategies.

    Args:
        name: Binary name (canonical or alias, e.g. "soffice" / "libreoffice").
        config: Optional full config dict. If provided, the function will
            check `binaries.<canonical>.path` as the highest-priority
            override.

    Returns:
        Absolute path to the binary as a string, or None if nothing was found.
        Callers should treat None as "tool unavailable" and degrade gracefully.
    """
    canonical = _resolve_alias(name)
    entry = _KNOWN_BINARIES.get(canonical, {})

    # 1. Explicit config override (highest priority)
    if config is not None:
        override = get_nested(config, f"binaries.{canonical}.path", None)
        if override:
            expanded = _expand(str(override))
            if Path(expanded).exists():
                return expanded
            # Override set but wrong → don't silently fall through;
            # return None so the caller surfaces the misconfiguration.
            # Rationale: a user who bothered to set an explicit path has
            # a reason; falling back to auto-discovery would hide their
            # mistake.
            return None

    # 2. Dedicated environment variables
    for env_var in entry.get("env_vars", []):
        env_value = os.environ.get(env_var)
        if env_value:
            expanded = _expand(env_value)
            if Path(expanded).exists():
                return expanded
            # Same rationale as above — explicit env var + broken path = bail.
            return None

    # 3. shutil.which on canonical name and aliases
    for lookup_name in [canonical, *entry.get("aliases", [])]:
        found = shutil.which(lookup_name)
        if found:
            return found

    # 4. Platform-specific known install paths
    platform_paths = entry.get("paths", {}).get(_platform_key(), [])
    if platform_paths:
        found_path = _first_existing(platform_paths)
        if found_path:
            return found_path

    return None
