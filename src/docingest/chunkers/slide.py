"""
Slide chunker — 1 slide = 1 chunk (PPT best practice).

Boundary detection priority (see ARCHITECTURE.md §5.6):
  1. Docling slide separator markers (if present)
  2. Horizontal rule "---" pattern
  3. "# Slide N" / "## スライド N" heading pattern
  4. All absent → fallback to recursive

Rules:
  - Each slide is an independent semantic unit
  - slide > max_tokens → recursive within slide
  - slide < min_tokens → keep as-is (don't merge across slides)
  - Speaker notes appended to slide chunk
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker


# Patterns for slide boundary detection (priority order)
_PAGEBREAK_RE = re.compile(r"^<!-- pagebreak\s*-->", re.IGNORECASE)
_DOCLING_SLIDE_RE = re.compile(r"^<!-- slide\b", re.IGNORECASE)
_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$")
_SLIDE_HEADING_RE = re.compile(
    r"^#{1,2}\s+(?:slide|スライド|page|ページ)\s*\d+",
    re.IGNORECASE,
)


class SlideChunker(BaseChunker):
    """Split PPTX-converted Markdown by slide boundaries."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._recursive = RecursiveChunker(config)

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        """Split by slides, fallback to recursive if no boundaries found."""
        if not markdown.strip():
            return []

        slides = self._split_slides(markdown)

        if not slides:
            # No slide boundaries detected → recursive fallback
            return self._recursive.chunk(markdown, metadata)

        # Build chunks from slides
        all_chunks: list[Chunk] = []

        for i, slide_text in enumerate(slides):
            slide_text = slide_text.strip()
            if not slide_text:
                continue

            # Extract slide title from first non-empty, non-comment line
            slide_title = self._extract_slide_title(slide_text)
            slide_meta = {
                **metadata,
                "slide_index": i,
                "title_path": slide_title,
            }

            tokens = self.estimate_tokens(slide_text)

            if tokens <= self._max_tokens:
                all_chunks.append(Chunk(
                    text=slide_text,
                    metadata=slide_meta,
                ))
            else:
                # Slide too large → subdivide
                sub_chunks = self._recursive.chunk(slide_text, slide_meta)
                all_chunks.extend(sub_chunks)

        # Renumber
        for idx, c in enumerate(all_chunks):
            c.metadata["chunk_index"] = idx
            c.metadata["total_chunks"] = len(all_chunks)
            c.metadata["tokens"] = self.estimate_tokens(c.text)

        return all_chunks

    def _split_slides(self, markdown: str) -> list[str]:
        """
        Detect slide boundaries and split.

        Tries detection methods in priority order.
        Returns empty list if no boundaries found (triggers fallback).
        """
        lines = markdown.split("\n")

        # Priority 0: Page break placeholder (from Docling export_to_markdown)
        boundaries = self._find_boundaries(lines, _PAGEBREAK_RE)
        if boundaries:
            return self._split_at(lines, boundaries)

        # Priority 1: Docling slide markers
        boundaries = self._find_boundaries(lines, _DOCLING_SLIDE_RE)
        if boundaries:
            return self._split_at(lines, boundaries)

        # Priority 2: Horizontal rules
        boundaries = self._find_boundaries(lines, _HR_RE)
        if boundaries:
            return self._split_at(lines, boundaries)

        # Priority 3: Slide heading pattern
        boundaries = self._find_boundaries(lines, _SLIDE_HEADING_RE)
        if boundaries:
            return self._split_at(lines, boundaries)

        # No boundaries found
        return []

    def _find_boundaries(
        self, lines: list[str], pattern: re.Pattern
    ) -> list[int]:
        """Find line indices matching boundary pattern."""
        return [i for i, line in enumerate(lines) if pattern.match(line)]

    def _split_at(self, lines: list[str], boundaries: list[int]) -> list[str]:
        """Split lines at boundary indices into slide texts."""
        if not boundaries:
            return []

        slides: list[str] = []

        # Content before first boundary (if any)
        if boundaries[0] > 0:
            pre = "\n".join(lines[:boundaries[0]]).strip()
            if pre:
                slides.append(pre)

        # Each boundary starts a new slide
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
            # Skip the boundary line itself if it's a pure separator (pagebreak/HR)
            content_start = start + 1 if _PAGEBREAK_RE.match(lines[start]) else start
            slide_text = "\n".join(lines[content_start:end]).strip()
            if slide_text:
                slides.append(slide_text)

        return slides

    @staticmethod
    def _extract_slide_title(text: str) -> str:
        """Extract title from slide: first non-empty, non-comment line."""
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("<!--"):
                continue
            # Strip heading markers
            return stripped.lstrip("#").strip()
        return ""
