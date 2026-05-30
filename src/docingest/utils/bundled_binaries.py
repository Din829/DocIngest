"""
Bundled-binary path injection — make DocIngest find the binaries that ship
*with* it (in a packaged exe or via the imageio-ffmpeg wheel), without the
user installing anything system-wide.

How it works
------------
`binary_finder.find_binary` already honours the env vars `FFMPEG_PATH` /
`FFPROBE_PATH` / `SOFFICE_PATH` (lookup-chain step 2). So the whole job here
is: at startup, if we can locate a bundled binary, point the matching env var
at it. find_binary then resolves to it — zero changes to find_binary itself.

Resolution order per binary (first hit wins):
  1. User already set the env var → leave it ALONE (respect explicit config).
  2. Packaged binary under PyInstaller's `sys._MEIPASS` (the exe case).
  3. ffmpeg only: the imageio-ffmpeg wheel's bundled binary.
  (System-installed binaries need no injection — find_binary's later steps
   find them. We only inject what we *bring*.)

Anything not found is simply not injected — find_binary falls through to its
normal discovery / graceful-degrade path. We never raise: a missing optional
binary is a known degrade (e.g. no ffprobe → no duration_sec), not an error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Subdirectory inside the packaged bundle where we drop binaries. The exe
# packaging step (--add-binary / --add-data) places them here; this is the
# single agreed name both sides reference.
_BUNDLE_SUBDIR = "_bundled_bin"

# Per-binary: the env var find_binary reads + the filename(s) to look for
# under the bundle dir (Windows .exe vs bare name).
_BUNDLED = {
    "FFMPEG_PATH": ("ffmpeg.exe", "ffmpeg"),
    "FFPROBE_PATH": ("ffprobe.exe", "ffprobe"),
    "SOFFICE_PATH": ("soffice.exe", "soffice"),
}


def _meipass_dir() -> Path | None:
    """The PyInstaller runtime extraction dir, or None when not packaged."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / _BUNDLE_SUBDIR if base else None


def _find_in_bundle(filenames: tuple[str, ...]) -> str | None:
    """Look for a binary under the packaged bundle dir (recursively, since
    LibreOffice ships as a tree, not a flat file)."""
    bundle = _meipass_dir()
    if bundle is None or not bundle.is_dir():
        return None
    for fn in filenames:
        # Top-level fast path
        direct = bundle / fn
        if direct.is_file():
            return str(direct)
        # LibreOffice lives at <bundle>/LibreOffice/program/soffice(.exe)
        for hit in bundle.rglob(fn):
            if hit.is_file():
                return str(hit)
    return None


def _imageio_ffmpeg_exe() -> str | None:
    """Path to the ffmpeg binary bundled in the imageio-ffmpeg wheel, if
    that package is installed. Returns None otherwise (we degrade)."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        # get_ffmpeg_exe can raise if the wheel has no binary for this
        # platform — treat as "not available", let find_binary degrade.
        return None
    return exe if exe and Path(exe).is_file() else None


def ensure_bundled_binaries() -> dict[str, str]:
    """Point env vars at the binaries we ship, so find_binary resolves to
    them. Call once at startup (after load_dotenv). Returns the mapping of
    env vars we actually set (for logging / tests). Idempotent and safe to
    call when nothing is bundled — then it sets nothing and returns {}.
    """
    injected: dict[str, str] = {}

    for env_var, filenames in _BUNDLED.items():
        # 1. Respect an explicit user/operator setting — never override.
        if os.environ.get(env_var):
            continue

        # 2. Packaged binary (the exe case).
        path = _find_in_bundle(filenames)

        # 3. ffmpeg-only fallback: the imageio-ffmpeg wheel.
        if path is None and env_var == "FFMPEG_PATH":
            path = _imageio_ffmpeg_exe()

        if path:
            os.environ[env_var] = path
            injected[env_var] = path

    return injected
