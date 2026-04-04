"""
Index builder — generates index.json for Agent file discovery.

The index.json serves as a "table of contents" for Agentic Search:
an Agent reads it to know what files exist, their types, and what
sections they contain — then decides which file to grep/glob.

Design:
  - Built incrementally (add_file called per processed file)
  - Written once at the end (write_index)
  - Includes stats summary for quick overview
"""

from __future__ import annotations

import json
import datetime
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult


def _extract_sections(markdown: str) -> list[str]:
    """
    Extract top-level section titles from Markdown.

    Looks for ## headings (H2) as main sections.
    Falls back to # (H1) if no H2 found.
    """
    sections = []
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            sections.append(stripped[3:].strip())

    # If no H2 found, try H1
    if not sections:
        for line in markdown.split("\n"):
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                sections.append(stripped[2:].strip())

    return sections


class IndexBuilder:
    """
    Builds index.json incrementally.

    Usage:
        builder = IndexBuilder(config)
        builder.add_file(parse_result, original_file, output_path)
        builder.add_file(...)
        builder.write_index(output_dir)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.files: list[dict[str, Any]] = []
        self.total_chunks = 0
        self.total_tokens = 0
        self.errors = 0

    def add_file(
        self,
        parse_result: ParseResult,
        original_file: Path,
        output_path: Path,
        output_dir: Path,
        chunks_count: int = 0,
    ) -> None:
        """
        Add a processed file to the index.

        Args:
            parse_result: Parse result with markdown and metadata.
            original_file: Original input file path.
            output_path: Absolute path to the written .md file.
            output_dir: Base output directory (for relative path calculation).
            chunks_count: Number of chunks generated for this file.
        """
        # Compute relative path from output_dir
        try:
            rel_path = str(output_path.relative_to(output_dir))
        except ValueError:
            rel_path = str(output_path)

        from ..chunkers.base import BaseChunker
        tokens_est = BaseChunker.estimate_tokens(parse_result.markdown)
        sections = _extract_sections(parse_result.markdown)
        metadata = parse_result.metadata

        entry: dict[str, Any] = {
            "path": rel_path.replace("\\", "/"),  # Normalize to forward slashes
            "original_file": original_file.name,
            "format": metadata.get("format", "unknown"),
            "title": metadata.get("title", original_file.stem),
            "language": metadata.get("language", ""),
            "tokens_estimated": tokens_est,
            "chunks_count": chunks_count,
        }

        # Optional fields (only include if available)
        if "pages" in metadata:
            entry["pages"] = metadata["pages"]
        if sections:
            entry["sections"] = sections
        if metadata.get("has_tables"):
            entry["has_tables"] = True
        if metadata.get("has_images"):
            entry["has_images"] = True

        self.files.append(entry)
        self.total_chunks += chunks_count
        self.total_tokens += tokens_est

    def add_error(self) -> None:
        """Record that a file failed processing."""
        self.errors += 1

    def write_index(self, output_dir: Path) -> Path:
        """
        Write index.json to output directory.

        Args:
            output_dir: Base output directory.

        Returns:
            Path to the written index.json.
        """
        index_filename = get_nested(self.config, "output.index_file", "index.json")
        index_path = output_dir / index_filename

        index_data = {
            "version": 1,
            "processed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "files": self.files,
            "stats": {
                "total_files": len(self.files),
                "total_chunks": self.total_chunks,
                "total_tokens": self.total_tokens,
                "errors": self.errors,
            },
        }

        index_path.write_text(
            json.dumps(index_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return index_path
