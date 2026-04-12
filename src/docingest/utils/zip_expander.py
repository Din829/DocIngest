"""
ZIP archive expansion for DocIngest input discovery.

Problem
-------
Users routinely hand DocIngest a `.zip` full of mixed documents (PDFs,
spreadsheets, images, nested zips). The pipeline is file-oriented
(every file → one `sources/*.md`), so the zip has to be flattened into
the file list *before* parsing starts.

Design
------
`expand_zip(zip_path, extract_root, config, depth=0)` unpacks a zip into
a deterministic subdirectory of `extract_root` and returns the list of
extracted file paths. Nested zips are recursed up to `max_nesting_depth`.

Key decisions
~~~~~~~~~~~~~
* **Content-based detection** (`is_zip_file`) — we use `zipfile.is_zipfile`
  on the byte signature, not the extension. A `.zip` that isn't really a
  zip gets passed through unchanged; a renamed zip still gets expanded.

* **Persistent extract root** (`{output.dir}/.cache/_zip_extract/`) — the
  second run reuses the extracted files as-is. Content-addressed cache
  still kicks in at the pipeline level.

* **Deterministic filename flattening** — each inner file is renamed to
  `<zip_stem>__<inner/path/slashes/flattened>`. This preserves enough
  provenance to trace a file back to its archive while sidestepping
  collisions across archives and cache-key name collisions.

* **Japanese filename recovery** — Windows-created zips encode filenames
  in CP932; Python's `zipfile` decodes them as CP437 by default and
  produces mojibake. We decode the raw bytes through a fallback chain
  (cp437 → cp932 → utf-8). The chain is configurable.

* **Bomb protection** — before extracting, we sum `ZipInfo.file_size`
  (the *uncompressed* size recorded in the central directory) and
  refuse the archive if it exceeds `max_total_extract_size_mb`. File
  count and nesting depth are bounded too. An attacker needs to beat
  three independent limits to do damage.

* **Graceful degradation** — any failure (`BadZipFile`, password, bomb,
  over-depth) logs a warning and returns `None`, which the pipeline
  interprets as "keep the original zip in the file list" — Docling will
  then fail loudly on it, preserving the explicit error in errors.json.

Non-goals
~~~~~~~~~
* We do not decrypt password-protected zips.
* We do not stream-process huge zips; the whole archive is walked once
  for the central directory read, then each entry is extracted to disk.
  For typical DocIngest input (MB-range archives) this is fine.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from ..config import get_nested

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_zip_file(path: Path) -> bool:
    """
    True iff `path` is a real zip archive (by byte signature).

    Unlike `path.suffix == ".zip"`, this also catches renamed zips and
    avoids treating `.docx` / `.xlsx` / `.pptx` as zips even though they
    technically are — the caller is responsible for not handing us
    office files. We double-check below.
    """
    try:
        if not path.is_file():
            return False
        return zipfile.is_zipfile(str(path))
    except OSError:
        return False


# Extensions that are structurally zips but should NOT be expanded here.
# DocIngest's Docling parser handles these directly and has format-specific
# enrichment (OMML preprocess, chart extraction, xlsx denoising). Expanding
# them would break that path.
_ZIPLIKE_NOT_EXPANDABLE = {
    ".docx", ".docm", ".dotx", ".dotm",
    ".pptx", ".pptm", ".ppsx", ".ppsm",
    ".xlsx", ".xlsm", ".xltx", ".xltm",
    ".odt", ".ods", ".odp",
    ".epub",  # zip-based but handled by its own parser (if enabled)
    ".jar",   # java archives
}


def should_expand(path: Path) -> bool:
    """Decide whether this zip-shaped file is a DocIngest-expandable archive."""
    if path.suffix.lower() in _ZIPLIKE_NOT_EXPANDABLE:
        return False
    return is_zip_file(path)


# ---------------------------------------------------------------------------
# Filename decoding (Japanese / UTF-8 / CP437 fallback chain)
# ---------------------------------------------------------------------------

def _decode_filename(raw_name: str, _info: zipfile.ZipInfo, encodings: list[str]) -> str:
    """
    Recover the original filename from a ZipInfo.

    Background
    ~~~~~~~~~~
    Real-world zips are a minefield of encoding inconsistencies:
      * The zip spec says filenames are CP437 unless a UTF-8 flag is set.
      * Windows zip tools frequently write CP932 bytes REGARDLESS of the
        flag (sometimes with the flag set even though the bytes aren't
        valid UTF-8).
      * Python's `zipfile` follows the spec literally and decodes as CP437
        unless the flag claims UTF-8 — producing mojibake for Japanese
        names from most Windows tools.

    The naive fix ("walk a fallback chain") fails if CP437 is in the chain
    because CP437 is an 8-bit codepage: every byte decodes successfully,
    so CP437 always "wins" even when the real encoding is CP932. The chain
    MUST therefore exclude CP437 — CP437 is what we're trying to escape.

    Strategy
    ~~~~~~~~
    1. Pure ASCII fast path — return unchanged (the common case).
    2. Recover the raw bytes by re-encoding `raw_name` as CP437. This
       undoes `zipfile`'s default decode and gives us the bytes from disk.
    3. Walk the configured encoding chain. Accept the first encoding that
       decodes without error and without replacement characters.
    4. If the chain has nothing that fits, fall back to Python's original
       decode so we at least have a non-empty filename.

    Configuring
    ~~~~~~~~~~~
    `parsing.zip.filename_encodings` defaults to `[utf-8, cp932, shift_jis,
    euc-jp]` — CJK-biased because that's the biggest pain point. CP437 is
    deliberately not in the default. Users can extend the list for other
    languages (cp949 for Korean, big5 for Traditional Chinese, etc.).
    """
    # Fast path: pure ASCII names are almost always correct already.
    if raw_name.isascii() and all(ord(c) >= 0x20 for c in raw_name):
        return raw_name

    # Recover the raw filename bytes. `zipfile` decoded them using either
    # CP437 (default) or UTF-8 (if the flag claimed so). In both cases
    # `raw_name.encode('cp437')` fails only if Python picked UTF-8 AND
    # the string contains non-CP437 codepoints — meaning Python already
    # did the right thing.
    try:
        raw_bytes = raw_name.encode("cp437")
    except UnicodeEncodeError:
        return raw_name  # Python's UTF-8 decode was valid; trust it.

    # Walk the fallback chain. Accept the first lossless decode.
    for enc in encodings:
        if enc.lower() == "cp437":
            # Sanity: CP437 must never be in the chain. If a user wrote
            # it into config by mistake, skip it silently to keep the
            # chain meaningful.
            continue
        try:
            decoded = raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\ufffd" in decoded:
            continue
        return decoded

    # Chain exhausted — fall back to Python's original result so we
    # return a non-empty filename. Mojibake is better than silence.
    return raw_name


# ---------------------------------------------------------------------------
# Name flattening (inner zip path → safe filesystem filename)
# ---------------------------------------------------------------------------

# Characters unsafe for Windows / cross-platform filenames.
_UNSAFE_CHARS = '<>:"|?*'


def _flatten_inner_path(zip_stem: str, inner_path: str) -> str:
    """
    Convert `subdir/file.pdf` inside `archive.zip` → `archive__subdir_file.pdf`.

    Preserves the original file suffix so downstream format detection keeps
    working. Handles nested directories by replacing slashes with underscores.
    """
    # Split stem and suffix so we can sanitise the stem only.
    p = Path(inner_path)
    inner_stem = p.with_suffix("").as_posix().replace("/", "_")
    suffix = p.suffix  # include the dot, preserve case

    # Remove any path-unsafe chars (preserve Unicode / CJK).
    for c in _UNSAFE_CHARS:
        inner_stem = inner_stem.replace(c, "_")

    return f"{zip_stem}__{inner_stem}{suffix}"


# ---------------------------------------------------------------------------
# Bomb defence
# ---------------------------------------------------------------------------

def _precheck_zip(
    infolist: list[zipfile.ZipInfo],
    max_total_bytes: int,
    max_file_count: int,
) -> tuple[bool, str]:
    """
    Inspect the central directory for obvious abuse before extracting.

    Returns (ok, reason). `ok=False` → caller should decline to expand.
    """
    # File-count limit
    if len(infolist) > max_file_count:
        return False, (
            f"archive contains {len(infolist)} entries "
            f"(limit {max_file_count})"
        )

    # Declared uncompressed total
    total_uncompressed = sum(info.file_size for info in infolist if not info.is_dir())
    if total_uncompressed > max_total_bytes:
        mb = total_uncompressed / (1024 * 1024)
        limit_mb = max_total_bytes / (1024 * 1024)
        return False, (
            f"archive expands to {mb:.1f} MB "
            f"(limit {limit_mb:.0f} MB) — likely a zip bomb"
        )

    # Also check compression ratio as a secondary signal: if any single
    # entry has > 1000x ratio it's very suspicious.
    for info in infolist:
        if info.is_dir() or info.compress_size == 0:
            continue
        ratio = info.file_size / info.compress_size
        if ratio > 1000 and info.file_size > 10 * 1024 * 1024:
            return False, (
                f"entry {info.filename!r} has compression ratio "
                f"{ratio:.0f}x — likely a zip bomb"
            )

    return True, ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def expand_zip(
    zip_path: Path,
    extract_root: Path,
    config: dict[str, Any],
    depth: int = 0,
) -> list[Path] | None:
    """
    Extract a zip archive into a deterministic subdirectory of extract_root
    and return the list of produced file paths.

    Returns None on any failure (caller keeps the original zip in the file
    list — Docling will surface the error explicitly). Nested zips inside
    the archive are recursed up to `parsing.zip.max_nesting_depth`.

    Args:
        zip_path: absolute path to the zip file.
        extract_root: base directory under which zips are expanded. Each
            archive gets its own subdirectory named after its stem.
        config: full DocIngest config dict.
        depth: current recursion depth (internal, do not set manually).
    """
    max_depth = int(get_nested(config, "parsing.zip.max_nesting_depth", 3))
    if depth >= max_depth:
        logger.warning(
            f"ZIP nesting depth limit reached ({max_depth}); leaving "
            f"{zip_path.name} unexpanded"
        )
        return None

    max_size_mb = int(get_nested(config, "parsing.zip.max_total_extract_size_mb", 500))
    max_file_count = int(get_nested(config, "parsing.zip.max_file_count", 10000))
    # CP437 deliberately omitted — it's what we're trying to escape.
    # See `_decode_filename` docstring for the full rationale.
    encodings = get_nested(
        config,
        "parsing.zip.filename_encodings",
        ["utf-8", "cp932", "shift_jis", "euc-jp"],
    )

    target_dir = extract_root / zip_path.stem
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            infolist = zf.infolist()

            ok, reason = _precheck_zip(
                infolist, max_size_mb * 1024 * 1024, max_file_count
            )
            if not ok:
                logger.warning(f"Refusing to expand {zip_path.name}: {reason}")
                return None

            extracted_files: list[Path] = []

            for info in infolist:
                if info.is_dir():
                    continue

                # Recover the real filename (Japanese support).
                real_name = _decode_filename(info.filename, info, encodings)

                # Skip macOS __MACOSX/ noise and hidden files.
                if real_name.startswith("__MACOSX/") or "/.DS_Store" in real_name:
                    continue
                if Path(real_name).name.startswith("._"):
                    continue

                # Flatten the inner path to a safe filesystem name.
                out_name = _flatten_inner_path(zip_path.stem, real_name)
                out_path = target_dir / out_name

                # Refuse overwrites that would cross extract_root (zip slip).
                try:
                    out_path.resolve().relative_to(extract_root.resolve())
                except ValueError:
                    logger.warning(
                        f"Zip slip attempt blocked: {info.filename!r} "
                        f"in {zip_path.name}"
                    )
                    continue

                # Extract the file content. We read() instead of extract()
                # so we can control the output path and avoid issues with
                # ZipFile.extract's normalisation of special characters.
                try:
                    data = zf.read(info.filename)
                except (RuntimeError, zipfile.BadZipFile) as e:
                    # RuntimeError is raised for encrypted entries.
                    logger.warning(
                        f"Cannot extract {info.filename!r} from "
                        f"{zip_path.name}: {e}"
                    )
                    continue

                out_path.write_bytes(data)
                extracted_files.append(out_path)

    except zipfile.BadZipFile as e:
        logger.warning(f"Corrupt zip {zip_path.name}: {e}")
        return None
    except OSError as e:
        logger.warning(f"OS error expanding {zip_path.name}: {e}")
        return None

    # Recurse into nested zips. We do this in a second pass so the outer
    # extract call doesn't mutate the list while iterating.
    final_files: list[Path] = []
    for p in extracted_files:
        if should_expand(p):
            inner_files = expand_zip(p, extract_root, config, depth=depth + 1)
            if inner_files is not None:
                final_files.extend(inner_files)
                # We keep the intermediate zip on disk for debugging but
                # drop it from the discovered file list — its contents
                # are already flattened in.
            else:
                # Recursion bailed; keep the original inner zip in the
                # list so the error surfaces through the pipeline.
                final_files.append(p)
        else:
            final_files.append(p)

    logger.info(
        f"Expanded {zip_path.name}: "
        f"{len(final_files)} file(s) → {target_dir}"
    )
    return final_files


# ---------------------------------------------------------------------------
# Extract root lifecycle
# ---------------------------------------------------------------------------

def get_extract_root(config: dict[str, Any]) -> Path:
    """
    Resolve the directory under which zip archives are expanded.

    Default: `{output.dir}/.cache/_zip_extract/`. Persistent across runs
    (lifecycle strategy B) so repeat runs reuse the extracted files and
    content-addressed caching can kick in.
    """
    output_dir = Path(get_nested(config, "output.dir", "./knowledge"))
    cache_dir_name = get_nested(config, "incremental.cache_dir", ".cache")
    zip_dir = get_nested(config, "parsing.zip.extract_dir", "_zip_extract")
    return output_dir / cache_dir_name / zip_dir


def clean_extract_root(config: dict[str, Any]) -> None:
    """
    Remove all expanded zip content. Exposed for `--force` or explicit
    cache-clear operations; not called automatically.
    """
    root = get_extract_root(config)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
        logger.info(f"Cleared zip extract root: {root}")
