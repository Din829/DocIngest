"""
Heading-based chunker — splits by Markdown headings, then recursive within sections.

Strategy:
  1. Split document at heading boundaries (configurable levels: H1, H2, H3)
  2. Each section becomes a candidate chunk
  3. If section > max_tokens → subdivide with recursive chunker
  4. If section < min_tokens → keep as-is (headings are semantic boundaries)

Key rules (from DESIGN.md):
  - Overlap applies WITHIN sections (recursive), NOT between sections
  - Section boundaries are hard (headings = semantic breaks)
  - Short sections are preserved (don't merge across headings)

This is the "single biggest improvement" for structured documents
per Firecrawl 2026 and PreMai 2026 guides.
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker


# Heading pattern: # Title, ## Title, ### Title, etc.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


class HeadingChunker(BaseChunker):
    """Split by Markdown headings, recursive within sections."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)

        heading_cfg = self._chunking.get("heading", {})
        self._levels = set(heading_cfg.get("levels", [1, 2, 3]))
        self._fallback_strategy = heading_cfg.get("fallback", "recursive")

        # Recursive chunker for subdividing large sections
        self._recursive = RecursiveChunker(config)

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        """Split by headings, then recursive within sections."""
        if not markdown.strip():
            return []

        # Split into sections by heading
        sections = self._split_by_headings(markdown)

        if not sections:
            # No headings found → fall back to recursive
            return self._recursive.chunk(markdown, metadata)

        # Process each section
        all_chunks: list[Chunk] = []

        for section in sections:
            section_text = section["text"]
            section_tokens = self.estimate_tokens(section_text)

            if section_tokens <= self._max_tokens:
                # Section fits → single chunk
                all_chunks.append(Chunk(text=section_text, metadata={
                    **metadata,
                    "title_path": section["title_path"],
                }))
            else:
                # Section too large → subdivide with recursive
                sub_chunks = self._recursive.chunk(section_text, {
                    **metadata,
                    "title_path": section["title_path"],
                })
                all_chunks.extend(sub_chunks)

        # Renumber chunk indices
        for i, c in enumerate(all_chunks):
            c.metadata["chunk_index"] = i
            c.metadata["total_chunks"] = len(all_chunks)
            c.metadata["tokens"] = self.estimate_tokens(c.text)

        return all_chunks

    def _split_by_headings(self, markdown: str) -> list[dict]:
        """
        Split markdown at heading boundaries.

        Returns list of {"text": "...", "title_path": "Section > Subsection"}.
        """
        lines = markdown.split("\n")
        sections: list[dict] = []
        current_lines: list[str] = []
        title_stack: list[tuple[int, str]] = []  # (level, title)
        current_title_path = ""

        for line in lines:
            match = _HEADING_RE.match(line)
            if match:
                level = len(match.group(1))  # Number of # signs
                title = match.group(2).strip()

                # Only split on configured heading levels
                if level in self._levels:
                    # Flush previous section
                    if current_lines:
                        text = "\n".join(current_lines).strip()
                        if text:
                            sections.append({
                                "text": text,
                                "title_path": current_title_path,
                            })
                        current_lines = []

                    # Update title stack
                    # Pop titles at same or deeper level
                    while title_stack and title_stack[-1][0] >= level:
                        title_stack.pop()
                    title_stack.append((level, title))

                    # Build title path: "Chapter > Section > Subsection"
                    current_title_path = " > ".join(t for _, t in title_stack)

            current_lines.append(line)

        # Flush last section
        if current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                sections.append({
                    "text": text,
                    "title_path": current_title_path,
                })

        return sections
