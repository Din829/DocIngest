"""
Markdown writer — writes parsed documents to sources/ with YAML frontmatter.

Takes a ParseResult (in-memory Markdown + metadata) and writes it to disk
as a properly formatted Markdown file with frontmatter header.

Design:
  - Frontmatter is optional (config: output.markdown.include_metadata_header)
  - Large files can be split (config: output.markdown.max_file_size_mb)
  - Output filename derived from original file stem
  - Duplicate filenames get numeric suffix (_1, _2, etc.)
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult


def _build_frontmatter(metadata: dict[str, Any], original_file: str) -> str:
    """
    Build YAML frontmatter string from metadata.

    Example output:
        ---
        source: report.pdf
        format: pdf
        title: Annual Report 2025
        language: ja
        pages: 120
        processed_at: 2026-04-02T10:30:00
        ---
    """
    lines = ["---"]

    lines.append(f"source: {original_file}")

    if "format" in metadata:
        lines.append(f"format: {metadata['format']}")
    if "title" in metadata:
        lines.append(f"title: {metadata['title']}")
    if "language" in metadata:
        lines.append(f"language: {metadata['language']}")
    if "pages" in metadata:
        lines.append(f"pages: {metadata['pages']}")

    lines.append(f"processed_at: {datetime.datetime.now().isoformat(timespec='seconds')}")

    lines.append("---")
    return "\n".join(lines)


def _resolve_output_path(sources_dir: Path, stem: str, existing: set[str]) -> Path:
    """
    Resolve output file path, avoiding duplicates.

    If "report.md" already exists, returns "report_1.md", "report_2.md", etc.
    """
    name = f"{stem}.md"
    if name not in existing:
        existing.add(name)
        return sources_dir / name

    # Find next available suffix
    counter = 1
    while True:
        name = f"{stem}_{counter}.md"
        if name not in existing:
            existing.add(name)
            return sources_dir / name
        counter += 1


def write_markdown(
    parse_result: ParseResult,
    original_file: Path,
    output_dir: Path,
    config: dict[str, Any],
    existing_names: set[str] | None = None,
) -> Path:
    """
    Write a ParseResult to a Markdown file in sources/.

    Args:
        parse_result: Parsed document (markdown + metadata).
        original_file: Original input file path.
        output_dir: Base output directory (e.g. ./knowledge/).
        config: Full config dict.
        existing_names: Set of already-used output filenames (for dedup).
            Mutated in-place when a new name is added.

    Returns:
        Path to the written .md file (relative to output_dir).
    """
    if existing_names is None:
        existing_names = set()

    sources_dir = output_dir / get_nested(config, "output.sources_dir", "sources")
    sources_dir.mkdir(parents=True, exist_ok=True)

    # Resolve output path (handle duplicates)
    output_path = _resolve_output_path(sources_dir, original_file.stem, existing_names)

    # Build content
    parts: list[str] = []

    # Frontmatter (optional)
    include_header = get_nested(config, "output.markdown.include_metadata_header", True)
    if include_header:
        frontmatter = _build_frontmatter(parse_result.metadata, original_file.name)
        parts.append(frontmatter)
        parts.append("")  # blank line after frontmatter

    # Main content
    parts.append(parse_result.markdown)

    content = "\n".join(parts)

    # Write
    output_path.write_text(content, encoding="utf-8")

    return output_path
