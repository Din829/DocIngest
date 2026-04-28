"""
File metadata enrichment hook.

Two sources, same destination (parse_result.metadata):

  1. Docling origin:
     Docling's DoclingDocument exposes origin.filename / mimetype /
     binary_hash — these are already harvested by DoclingParser into
     metadata["docling_origin"], this hook just promotes the useful ones
     to top-level keys (so the frontmatter writer doesn't need to know
     about the nested structure).

  2. exiftool (optional):
     Extracts author / creation date / EXIF / GPS / camera info that
     Docling does NOT expose. Requires:
       - `exiftool` executable on PATH (or DOCINGEST__metadata__exiftool__path)
       - `pyexiftool` Python package (optional dependency)
     When either is missing, this part is silently skipped.

Design:
  - The hook never raises. If exiftool is unavailable, we still surface
    the Docling origin fields (which are free).
  - CVE-2021-22204 version check: refuse exiftool < 12.24.
  - Results go into parse_result.metadata["exif"] as a flat dict, and
    a few high-value fields are also promoted to top-level
    (author, created_at) for easier frontmatter rendering.

Config (all under metadata.exiftool):
  enabled        — master toggle (default false)
  path           — override exiftool discovery
  fields         — per-format whitelist of field names to keep
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult
from ..utils.binary_finder import find_binary

logger = logging.getLogger(__name__)


# Default field whitelists per format family. Users can override via
# `metadata.exiftool.fields.<format>` in YAML. Keys are chosen to match
# ExifTool's canonical field names.
_DEFAULT_FIELDS: dict[str, list[str]] = {
    "image": [
        "ImageSize", "Title", "Caption", "Description", "Keywords",
        "Artist", "Author", "DateTimeOriginal", "CreateDate", "GPSPosition",
    ],
    "pdf": [
        "Title", "Author", "Subject", "Keywords",
        "CreateDate", "ModifyDate", "Producer", "Creator",
    ],
    "audio": [
        "Title", "Artist", "Album", "Genre", "Track",
        "DateTimeOriginal", "CreateDate", "Duration",
    ],
    "office": [
        "Title", "Author", "Subject", "Keywords", "Company",
        "CreateDate", "ModifyDate", "LastModifiedBy",
    ],
}

_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "tiff", "bmp", "webp", "heic"}
_AUDIO_EXTS = {"wav", "mp3", "m4a", "flac", "ogg", "aac"}
_OFFICE_EXTS = {"docx", "doc", "pptx", "ppt", "xlsx", "xls"}


def _classify(ext: str) -> str:
    """Classify extension into a field-whitelist family."""
    e = ext.lstrip(".").lower()
    if e in _IMAGE_EXTS:
        return "image"
    if e == "pdf":
        return "pdf"
    if e in _AUDIO_EXTS:
        return "audio"
    if e in _OFFICE_EXTS:
        return "office"
    return "image"  # sensible default: image whitelist is broadest


# ---------------------------------------------------------------------------
# Docling origin promotion (always runs — free)
# ---------------------------------------------------------------------------

def _promote_docling_origin(parse_result: ParseResult) -> None:
    """
    Copy useful fields from metadata["docling_origin"] to top-level keys.

    This keeps the frontmatter writer (markdown_writer.py) simple — it
    only needs to know about flat keys.
    """
    origin = parse_result.metadata.get("docling_origin")
    if not isinstance(origin, dict):
        return

    # Docling gives us filename / mimetype / binary_hash. The filename
    # is usually redundant with `source` in frontmatter, but mimetype
    # and binary_hash are genuinely new info.
    if "mimetype" in origin and "mimetype" not in parse_result.metadata:
        parse_result.metadata["mimetype"] = origin["mimetype"]
    if "binary_hash" in origin and "binary_hash" not in parse_result.metadata:
        # binary_hash is useful for deduplication downstream
        parse_result.metadata["binary_hash"] = origin["binary_hash"]


# ---------------------------------------------------------------------------
# ExifTool CVE version check
# ---------------------------------------------------------------------------
# Path discovery is handled by utils.binary_finder.find_binary("exiftool"),
# which knows about Windows Program Files installs, env var overrides, and
# config-level pinning via `binaries.exiftool.path`. The legacy
# `metadata.exiftool.path` override is still honoured for backwards
# compatibility via the config shim in file_metadata_hook below.

_MIN_EXIFTOOL_VERSION = (12, 24)  # CVE-2021-22204 fix


def _check_exiftool_version(path: str) -> bool:
    """Verify exiftool >= 12.24 (CVE-2021-22204 fix). Logs and returns bool."""
    import subprocess
    try:
        out = subprocess.run(
            [path, "-ver"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        parts = tuple(int(x) for x in out.split("."))
        if parts < _MIN_EXIFTOOL_VERSION:
            logger.warning(
                f"exiftool {out} is vulnerable to CVE-2021-22204. "
                f"Require >= 12.24. Skipping metadata extraction."
            )
            return False
        return True
    except Exception as e:
        logger.debug(f"Could not verify exiftool version at {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# ExifTool extraction (optional — requires pyexiftool)
# ---------------------------------------------------------------------------

def _extract_exif(
    file_path: Path,
    exiftool_path: str,
    whitelist: list[str],
) -> dict[str, Any]:
    """
    Run exiftool via pyexiftool (stay_open mode) and return whitelisted fields.

    Falls back to subprocess if pyexiftool is not installed.
    """
    try:
        import exiftool  # type: ignore[import-not-found]
    except ImportError:
        return _extract_exif_subprocess(file_path, exiftool_path, whitelist)

    try:
        # ExifToolHelper manages the stay_open process; context manager
        # ensures proper shutdown even on error.
        with exiftool.ExifToolHelper(executable=exiftool_path) as et:  # type: ignore[attr-defined]
            results = et.get_metadata(str(file_path))
            if not results:
                return {}
            raw = results[0]
    except Exception as e:
        logger.debug(f"pyexiftool failed for {file_path.name}: {e}")
        return {}

    return _filter_fields(raw, whitelist)


def _extract_exif_subprocess(
    file_path: Path,
    exiftool_path: str,
    whitelist: list[str],
) -> dict[str, Any]:
    """Fallback: plain subprocess invocation (slower, no stay_open)."""
    import json
    import subprocess

    try:
        out = subprocess.run(
            [exiftool_path, "-json", "-n", str(file_path)],
            capture_output=True, check=True, timeout=30,
        ).stdout
        data = json.loads(out)
        if not data:
            return {}
        raw = data[0]
    except Exception as e:
        logger.debug(f"exiftool subprocess failed for {file_path.name}: {e}")
        return {}

    return _filter_fields(raw, whitelist)


def _filter_fields(raw: dict[str, Any], whitelist: list[str]) -> dict[str, Any]:
    """
    Reduce raw exiftool output to whitelisted fields.

    ExifTool prefixes field names with their group (e.g. 'EXIF:DateTimeOriginal',
    'XMP:Title'). The whitelist uses bare names — match across all groups and
    keep the first hit per bare name.
    """
    result: dict[str, Any] = {}
    seen: set[str] = set()

    for full_key, value in raw.items():
        # Strip group prefix: "EXIF:DateTimeOriginal" → "DateTimeOriginal"
        bare = full_key.split(":", 1)[-1]
        if bare in whitelist and bare not in seen:
            result[bare] = value
            seen.add(bare)

    return result


# ---------------------------------------------------------------------------
# Created date derivation (independent of exiftool — works without it)
# ---------------------------------------------------------------------------
# `created` answers "when was this document actually created", which is
# different from `processed_at` (when DocIngest ran). Multi-source with an
# explicit source tag so downstream readers (RAG / Bases / users) can
# decide whether to trust it.
#
# Priority chain (high → low):
#   1. embedded   — exiftool CreateDate / DateTimeOriginal (PDF, Office,
#                   image EXIF, audio ID3) OR yt-dlp upload_date promoted
#                   into metadata by MediaParser.
#   2. filesystem — file mtime (works for everything, but reflects when
#                   the file landed on this disk, not when it was authored).
#   3. (none)     — neither field is written. Empty beats wrong.
#
# `created_source` records which branch won. Always paired with `created`
# so a reader never sees a `created` value without knowing where it came
# from.

# Match exiftool's typical date format: "2024:08:15 09:30:00" (with
# optional sub-second / timezone tail). We accept the loose form because
# different formats use slightly different precisions.
_EXIFTOOL_DATE_RE = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)


def _normalize_iso(value: Any) -> str | None:
    """
    Coerce an exiftool / yt-dlp / unknown date string into ISO 8601.

    ExifTool emits "YYYY:MM:DD HH:MM:SS[+TZ]"; ISO wants "YYYY-MM-DDTHH:MM:SS".
    yt-dlp upload_date is "YYYYMMDD". Anything else we leave alone if it
    already parses, otherwise return None so the caller can fall through.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # yt-dlp compact form
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date().isoformat()
        except ValueError:
            return None

    # ExifTool form
    m = _EXIFTOOL_DATE_RE.match(s)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}"

    # Already ISO-ish — try parsing to confirm, but pass through original
    # string so we don't mangle a timezone the user already had.
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except ValueError:
        return None


def _derive_created(file_path: Path, parse_result: ParseResult) -> None:
    """
    Set `created` + `created_source` on parse_result.metadata.

    Runs unconditionally (no config gate). Skipping this when exiftool is
    off would mean the field never appears, which defeats the point —
    filesystem mtime is always available and is strictly more useful than
    nothing for time-based filtering.

    Existing `created` (e.g. set by a parser-specific path that already
    knows better) is respected — we never overwrite.
    """
    if "created" in parse_result.metadata:
        return  # someone upstream already filled it

    # 1. Embedded sources (in priority order)
    exif = parse_result.metadata.get("exif") or {}
    candidates: list[tuple[str, Any]] = [
        ("CreateDate", exif.get("CreateDate")),
        ("DateTimeOriginal", exif.get("DateTimeOriginal")),
        # MediaParser writes yt-dlp upload_date here for URL inputs.
        # (It's a MediaParser convention; absent for non-URL files.)
        ("yt_dlp_upload_date", parse_result.metadata.get("yt_dlp_upload_date")),
    ]
    for _label, raw in candidates:
        iso = _normalize_iso(raw)
        if iso:
            parse_result.metadata["created"] = iso
            parse_result.metadata["created_source"] = "embedded"
            return

    # 2. Filesystem fallback
    try:
        mtime = file_path.stat().st_mtime
        parse_result.metadata["created"] = (
            datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        )
        parse_result.metadata["created_source"] = "filesystem"
    except OSError as e:
        logger.debug(f"Could not stat {file_path.name} for created date: {e}")
        # Both fields stay absent — empty beats wrong.


# ---------------------------------------------------------------------------
# Entry point (called by hooks registry)
# ---------------------------------------------------------------------------

def file_metadata_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """
    Enrich parse_result.metadata with file-level metadata.

    Two phases:
      1. Always: promote Docling origin fields (free).
      2. Optional: run exiftool if enabled and available.

    Never raises — failures degrade gracefully.
    """
    # Phase 1: Free promotion (Docling already gave us the data)
    _promote_docling_origin(parse_result)

    # Phase 2: exiftool (gated by config + availability).
    # Done BEFORE _derive_created so its CreateDate / DateTimeOriginal
    # become the highest-priority source for the date derivation below.
    if get_nested(config, "metadata.exiftool.enabled", False):
        _run_exiftool(file_path, parse_result, config)

    # Phase 3: Derive `created` from whatever we've got (exif > mtime).
    # Runs unconditionally — every file deserves a created date for
    # downstream time-based filtering, even when exiftool is off.
    _derive_created(file_path, parse_result)


def _run_exiftool(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """ExifTool extraction extracted into its own helper so the main hook
    body stays linear and the new derivation phase is easier to read."""

    # Legacy config key `metadata.exiftool.path` takes precedence over the
    # new `binaries.exiftool.path` for backwards compatibility. If the
    # legacy key is set, inject it as the binary finder override so the
    # downstream code paths don't have to know about the shim.
    legacy_path = get_nested(config, "metadata.exiftool.path", None)
    finder_config: dict[str, Any] = config
    if legacy_path:
        finder_config = {
            **config,
            "binaries": {
                **(config.get("binaries") or {}),
                "exiftool": {"path": legacy_path},
            },
        }

    exiftool_path = find_binary("exiftool", finder_config)
    if not exiftool_path:
        logger.debug(
            f"exiftool not found for {file_path.name}; set binaries.exiftool.path, "
            f"EXIFTOOL_PATH env var, or install exiftool to enable metadata extraction"
        )
        return

    if not _check_exiftool_version(exiftool_path):
        return  # warning already logged

    ext = file_path.suffix.lstrip(".").lower()
    family = _classify(ext)

    # Resolve whitelist: user override > default for family
    user_fields = get_nested(config, f"metadata.exiftool.fields.{family}", None)
    whitelist = user_fields if isinstance(user_fields, list) else _DEFAULT_FIELDS[family]

    exif = _extract_exif(file_path, exiftool_path, whitelist)
    if not exif:
        return

    parse_result.metadata["exif"] = exif

    # Promote the most useful fields to top-level for frontmatter.
    # Author / created_at are the two that consistently matter across formats.
    for source_key, target_key in (
        ("Author", "author"),
        ("Artist", "author"),  # image family uses Artist
        ("CreateDate", "created_at"),
        ("DateTimeOriginal", "created_at"),
    ):
        if source_key in exif and target_key not in parse_result.metadata:
            parse_result.metadata[target_key] = exif[source_key]
