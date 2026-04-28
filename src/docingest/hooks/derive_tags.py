"""
Tags derivation hook (pre_write) — stage 1 of two.

Writes the easy, file-local part of `metadata["tags"]`:
  - format/<format>   (e.g. format/pdf)
  - lang/<language>   (e.g. lang/ja)

Stage 2 happens AFTER knowledge_map is built, in
`output/tags_enrichment.py`, which appends discriminative keywords from
the corpus-wide TF-IDF index. Stage 2 is its own pass over sources/*.md
(rewrite-in-place) because keyword discrimination is a corpus-level
signal that doesn't exist when this hook runs (pre-write, single file).

Why namespaced (`format/pdf` instead of `pdf`):
  - In Obsidian, "/" creates nested tags — users can collapse all
    `format/*` in the tag pane.
  - For RAG / grep, the prefix makes intent unambiguous: `tag:format/pdf`
    won't collide with a literal "pdf" keyword from a document body.
  - Drop the namespace for tools that don't want it via config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult
from . import HookNoOp

logger = logging.getLogger(__name__)


def derive_tags_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """Seed metadata["tags"] with format + language tags."""

    if not get_nested(config, "output.derived_metadata.tags.enabled", True):
        raise HookNoOp

    include_format = bool(
        get_nested(config, "output.derived_metadata.tags.include_format", True)
    )
    include_language = bool(
        get_nested(config, "output.derived_metadata.tags.include_language", True)
    )

    tags: list[str] = []

    if include_format:
        fmt = parse_result.metadata.get("format")
        if isinstance(fmt, str) and fmt.strip():
            tags.append(f"format/{fmt.strip().lower()}")

    if include_language:
        lang = parse_result.metadata.get("language")
        if isinstance(lang, str) and lang.strip():
            tags.append(f"lang/{lang.strip().lower()}")

    if not tags:
        raise HookNoOp  # nothing to write — keep the field absent

    # Merge with anything an upstream parser may have populated (rare,
    # but be defensive). Preserve ordering, dedup case-insensitively.
    existing = parse_result.metadata.get("tags")
    if isinstance(existing, list):
        seen = {str(t).lower() for t in existing if isinstance(t, str)}
        for t in tags:
            if t.lower() not in seen:
                existing.append(t)
                seen.add(t.lower())
        return

    parse_result.metadata["tags"] = tags
