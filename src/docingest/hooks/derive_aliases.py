"""
Aliases derivation hook (pre_write).

Produces metadata["aliases"] — a short, deduplicated list of names by
which downstream consumers (Obsidian, RAG queries, grep) can find this
document. Intentionally minimal: better to emit two clean names than five
noisy ones.

Source order (high → low):
  1. metadata["title"]            — set by parsers / Docling
  2. metadata["exif"]["Title"]    — present when exiftool ran
  3. file_path.stem               — last resort, the filename

Two filters:
  - garbage_patterns      — drop the candidate entirely (e.g. "Untitled",
                            "Microsoft Word - Document1")
  - temp_filename_patterns — DOWNGRADE the candidate (kept only when it's
                            the only thing we have); used for things like
                            "_v3", "(1)", "副本" — real names you might
                            still want to search by, but not preferred.

Why a separate hook (vs. dropping logic in markdown_writer): aliases are
content-derived metadata, same family as exif promotion. Putting it in a
hook keeps writer simple and lets users disable it cleanly.

Why pre_write: title from parser, exif from file_metadata_hook, and the
markdown body (for H1) are all in place by then — earlier phases would
miss exif.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult
from . import HookNoOp

logger = logging.getLogger(__name__)


# Conservative defaults — proven garbage strings and version-suffix
# patterns. Users can override / extend via
# `output.derived_metadata.aliases.{garbage,temp_filename}_patterns`.
_DEFAULT_GARBAGE: list[str] = [
    r"^untitled\d*$",
    r"^document\d*$",
    r"^microsoft word - ",
    r"^slide\d+$",
    r"^\s*$",
    # 64-char hex — sha256 / md5 etc. used as internal cache filenames
    # (e.g. pipeline.py Phase 0.5 .xls→.xlsx cache stems). Real user file
    # names never look like this; suppressing it stops the converted-file
    # stem from leaking into aliases.
    r"^[0-9a-f]{64}$",
]

_DEFAULT_TEMP: list[str] = [
    r"\bv\d+\b",            # _v3, v10
    r"\bfinal\b",
    r"_copy$",
    r"\(\d+\)$",            # (1), (2)
    r"^tmp[_\-]",
    r"副本",
    r"复件",
]


def _compile_patterns(raw: list[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for p in raw:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.warning(f"Invalid alias pattern ignored ({p!r}): {e}")
    return compiled


def _is_garbage(s: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(s) for p in patterns)


def _norm_for_dedup(s: str) -> str:
    """Collapse whitespace + lowercase for case-insensitive dedup. Keeps
    the original surface form as the emitted value — only the comparison
    key is normalized."""
    return re.sub(r"\s+", " ", s).strip().lower()


def derive_aliases_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """Populate metadata["aliases"] from title / exif.Title / filename."""

    if not get_nested(config, "output.derived_metadata.aliases.enabled", True):
        raise HookNoOp

    # Don't overwrite if a parser already produced a real list.
    existing = parse_result.metadata.get("aliases")
    if isinstance(existing, list) and existing:
        raise HookNoOp

    # Resolve patterns (config > defaults).
    garbage_raw = get_nested(
        config,
        "output.derived_metadata.aliases.garbage_patterns",
        _DEFAULT_GARBAGE,
    )
    temp_raw = get_nested(
        config,
        "output.derived_metadata.aliases.temp_filename_patterns",
        _DEFAULT_TEMP,
    )
    garbage_res = _compile_patterns(garbage_raw if isinstance(garbage_raw, list) else _DEFAULT_GARBAGE)
    temp_res = _compile_patterns(temp_raw if isinstance(temp_raw, list) else _DEFAULT_TEMP)

    max_count = int(
        get_nested(config, "output.derived_metadata.aliases.max_count", 5)
    )

    # Collect candidates. Each candidate is (surface_string, is_temp_flag).
    # is_temp = matched a temp_filename_pattern → demoted to last resort.
    candidates: list[tuple[str, bool]] = []

    title = parse_result.metadata.get("title")
    if isinstance(title, str) and title.strip():
        candidates.append((title.strip(), False))

    exif = parse_result.metadata.get("exif") or {}
    exif_title = exif.get("Title") if isinstance(exif, dict) else None
    if isinstance(exif_title, str) and exif_title.strip():
        candidates.append((exif_title.strip(), False))

    stem = file_path.stem.strip()
    if stem:
        is_temp = any(p.search(stem) for p in temp_res)
        candidates.append((stem, is_temp))

    # Filter garbage; bucket the rest into preferred / temp.
    preferred: list[str] = []
    temp: list[str] = []
    seen: set[str] = set()
    for surface, is_temp in candidates:
        if _is_garbage(surface, garbage_res):
            continue
        key = _norm_for_dedup(surface)
        if not key or key in seen:
            continue
        seen.add(key)
        (temp if is_temp else preferred).append(surface)

    # Preferred first; fall back to temp names only if we'd otherwise
    # have nothing (or there's headroom under max_count).
    if not preferred and temp:
        result = temp[:max_count]
    else:
        result = preferred[:max_count]
        if len(result) < max_count and temp:
            result.extend(temp[: max_count - len(result)])

    if not result:
        raise HookNoOp  # nothing useful — leave the field absent

    parse_result.metadata["aliases"] = result
