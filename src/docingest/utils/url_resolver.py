"""
URL → local file resolver (yt-dlp unified).

Handles ANY video/audio URL that yt-dlp supports (1000+ sites):
YouTube, Bilibili, NicoNico, Twitter/X, TikTok, Vimeo, 抖音, ...

One `yt-dlp` command does three things simultaneously:
  1. Extract audio as WAV (for ASR)
  2. Download subtitles in all available languages (SRT)
  3. Download metadata (info.json with title/uploader/description/tags)

The caller (media_parser) decides whether to use the subtitle or ASR.

For direct media URLs (not a video platform page — e.g.
https://example.com/audio.mp3), we skip yt-dlp and use a plain HTTP
download instead.

Design
------
  * Persistent download cache at `{output.dir}/.cache/_media/<url_hash>/`.
    Same URL on second run → reuses existing files (zero download).
  * yt-dlp binary resolved via binary_finder (handles PATH, env var,
    platform-specific paths).
  * All yt-dlp arguments are configurable via `parsing.url.yt_dlp_extra_args`.
  * Graceful degradation: yt-dlp missing → warning + return None.
    Direct URL download failure → warning + return None.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..utils.binary_finder import find_binary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

def is_url(value: str) -> bool:
    """True if the string looks like an HTTP(S) URL."""
    stripped = value.strip()
    return stripped.startswith("http://") or stripped.startswith("https://")


def _is_direct_media_url(url: str) -> bool:
    """
    Heuristic: is this a direct link to a media file rather than a video
    platform page? Direct URLs typically end in a media extension.
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    media_exts = {
        ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma",
        ".mp4", ".avi", ".mkv", ".webm", ".mov", ".wmv",
    }
    return any(path.endswith(ext) for ext in media_exts)


def _url_hash(url: str) -> str:
    """Short deterministic hash for cache directory naming."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Direct media download (simple HTTP GET, no yt-dlp needed)
# ---------------------------------------------------------------------------

def _download_direct(url: str, download_dir: Path) -> list[Path]:
    """Download a direct media URL via HTTP GET."""
    import requests
    from urllib.parse import urlparse

    filename = Path(urlparse(url).path).name or "media"
    dest = download_dir / filename

    if dest.exists() and dest.stat().st_size > 0:
        logger.debug(f"Direct download cache hit: {dest}")
        return [dest]

    try:
        resp = requests.get(url, stream=True, timeout=300)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        logger.info(f"Downloaded {url} → {dest.name} ({dest.stat().st_size} bytes)")
        return [dest]
    except Exception as e:
        logger.warning(f"Direct download failed for {url}: {e}")
        return []


# ---------------------------------------------------------------------------
# JavaScript runtime detection (needed by yt-dlp for YouTube etc.)
# ---------------------------------------------------------------------------

# yt-dlp >= 2026 requires a JS runtime for some extractors (notably YouTube).
# It defaults to deno only, but most dev machines have node or bun instead.
# We detect what's available and pass --js-runtimes so yt-dlp can use it.
_JS_RUNTIMES_CACHE: list[str] | None = None


def _detect_js_runtimes() -> list[str]:
    """
    Detect which JS runtimes are available on this machine.

    Returns a list of runtime names that yt-dlp accepts for --js-runtimes
    (e.g. ["node", "bun", "deno"]). Cached after first call.
    """
    global _JS_RUNTIMES_CACHE
    if _JS_RUNTIMES_CACHE is not None:
        return _JS_RUNTIMES_CACHE

    import shutil
    available: list[str] = []
    # Order: node (most common), deno, bun. We pick the first available.
    for runtime in ("node", "deno", "bun"):
        if shutil.which(runtime):
            available.append(runtime)

    _JS_RUNTIMES_CACHE = available
    if available:
        logger.debug(f"JS runtimes for yt-dlp: {available}")
    else:
        logger.debug("No JS runtimes found (deno/node/bun); yt-dlp may fail on some sites")
    return available


# ---------------------------------------------------------------------------
# yt-dlp download (video platforms — YouTube, Bilibili, etc.)
# ---------------------------------------------------------------------------

def _download_ytdlp(
    url: str,
    download_dir: Path,
    config: dict[str, Any],
) -> list[Path]:
    """
    Download audio + subtitles + metadata via yt-dlp.

    Returns list of produced files (typically: .wav + .srt + .info.json).
    Empty list on failure.
    """
    yt_dlp = find_binary("yt-dlp", config)
    if not yt_dlp:
        logger.warning(
            "yt-dlp not found; cannot download video URLs. "
            "Install yt-dlp to enable URL support."
        )
        return []

    ffmpeg_path = find_binary("ffmpeg", config)

    # Build yt-dlp command
    output_template = str(download_dir / "%(id)s.%(ext)s")
    cmd = [
        yt_dlp,
        "--extract-audio",
        "--audio-format", "mp3",     # mp3 is ~10x smaller than WAV, all ASR engines accept it
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "all",
        "--write-info-json",
        "--no-playlist",           # single video only
        "--output", output_template,
    ]

    # If we found ffmpeg, tell yt-dlp where it is
    if ffmpeg_path:
        ffmpeg_dir = str(Path(ffmpeg_path).parent)
        cmd.extend(["--ffmpeg-location", ffmpeg_dir])

    # Auto-detect available JavaScript runtimes for yt-dlp.
    # yt-dlp >= 2026 defaults to deno-only, but many machines have node
    # or bun instead. Without this flag, YouTube extraction fails with
    # "No supported JavaScript runtime could be found". We probe for all
    # known runtimes and tell yt-dlp to use whatever is available, so
    # the pipeline works on any machine regardless of which runtime is
    # installed.
    # Pass only the first available runtime — yt-dlp only needs one, and
    # some versions have issues with comma-separated lists.
    js_runtimes = _detect_js_runtimes()
    if js_runtimes:
        cmd.extend(["--js-runtimes", js_runtimes[0]])

    # User-configurable extra args (can override anything above)
    extra_args = get_nested(config, "parsing.url.yt_dlp_extra_args", [])
    if isinstance(extra_args, list):
        cmd.extend(extra_args)

    cmd.append(url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max for download
        )
        if result.returncode != 0:
            logger.warning(
                f"yt-dlp failed for {url}: {result.stderr[:500]}"
            )
            # Don't return empty — there might be partial files
    except subprocess.TimeoutExpired:
        logger.warning(f"yt-dlp timed out for {url}")
    except Exception as e:
        logger.warning(f"yt-dlp error for {url}: {e}")

    # Collect whatever files yt-dlp produced
    produced = sorted(download_dir.glob("*"))
    # Filter out directories and zero-byte files
    produced = [f for f in produced if f.is_file() and f.stat().st_size > 0]

    if produced:
        logger.info(
            f"yt-dlp produced {len(produced)} file(s) for {url}: "
            f"{[f.name for f in produced]}"
        )
    else:
        logger.warning(f"yt-dlp produced no files for {url}")

    return produced


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_url(
    url: str,
    config: dict[str, Any],
) -> list[Path] | None:
    """
    Download a URL to local files and return the file list.

    Returns None if the URL cannot be resolved (caller should skip).
    Returns a list of Paths (audio + subtitles + metadata) on success.

    The download is cached at `{output.dir}/.cache/_media/<url_hash>/`.
    Same URL on second run → reuses existing files.
    """
    if not get_nested(config, "parsing.url.enabled", True):
        return None

    # Resolve download directory
    output_dir = Path(get_nested(config, "output.dir", "./knowledge"))
    cache_dir_name = get_nested(config, "incremental.cache_dir", ".cache")
    media_dir = get_nested(config, "parsing.url.download_dir", "_media")
    download_dir = output_dir / cache_dir_name / media_dir / _url_hash(url)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Check cache: if the directory already has files, reuse them
    existing = [
        f for f in download_dir.glob("*")
        if f.is_file() and f.stat().st_size > 0
    ]
    if existing:
        logger.debug(f"URL cache hit for {url}: {len(existing)} file(s)")
        return existing

    # Route: direct media URL vs video platform
    if _is_direct_media_url(url):
        files = _download_direct(url, download_dir)
    else:
        files = _download_ytdlp(url, download_dir, config)

    return files if files else None


def get_media_cache_root(config: dict[str, Any]) -> Path:
    """Resolve the media download cache root directory."""
    output_dir = Path(get_nested(config, "output.dir", "./knowledge"))
    cache_dir_name = get_nested(config, "incremental.cache_dir", ".cache")
    media_dir = get_nested(config, "parsing.url.download_dir", "_media")
    return output_dir / cache_dir_name / media_dir
