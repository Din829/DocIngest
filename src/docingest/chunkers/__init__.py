"""
Document chunkers — Phase 3: Markdown → chunks.

Auto strategy routing:
  1. Check document source format (from metadata)
  2. If PPTX → slide, XLSX/CSV → sheet, image → whole
  3. Otherwise → structure scoring → heading or recursive

All thresholds are configurable (no hardcoded values).
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker
from .heading import HeadingChunker
from ..config import get_nested


# ---------------------------------------------------------------------------
# Structure scoring for auto strategy
# ---------------------------------------------------------------------------

def _count_headings(markdown: str) -> int:
    """Count Markdown headings (# to ###)."""
    return len(re.findall(r"^#{1,3}\s+.+$", markdown, re.MULTILINE))


def _check_heading_gaps(markdown: str, max_gap: int = 2) -> bool:
    """
    Check if heading levels don't jump too much.

    Returns True if headings are well-structured (no big jumps).
    """
    levels = []
    for match in re.finditer(r"^(#{1,6})\s+", markdown, re.MULTILINE):
        levels.append(len(match.group(1)))

    if len(levels) < 2:
        return True  # Too few headings to judge

    for i in range(1, len(levels)):
        # Going deeper: check gap
        if levels[i] > levels[i - 1]:
            gap = levels[i] - levels[i - 1]
            if gap > max_gap:
                return False
    return True


def _check_heading_content_sizes(
    markdown: str,
    min_tokens: int = 100,
    max_tokens: int = 2000,
    pass_ratio: float = 0.5,
) -> bool:
    """
    Check if content between headings is reasonably sized.

    Returns True if most sections have appropriate content length.
    """
    sections = re.split(r"^#{1,3}\s+.+$", markdown, flags=re.MULTILINE)
    if len(sections) < 2:
        return False

    reasonable = 0
    for section in sections[1:]:  # Skip content before first heading
        tokens = BaseChunker.estimate_tokens(section.strip())
        if min_tokens <= tokens <= max_tokens:
            reasonable += 1

    return reasonable >= len(sections[1:]) * pass_ratio


def compute_structure_score(markdown: str, config: dict[str, Any]) -> int:
    """
    Compute structure quality score for auto strategy decision.

    Score 0-3:
      +1 if heading count >= min_headings
      +1 if heading levels don't jump
      +1 if heading content sizes are reasonable

    All thresholds configurable via chunking.auto.* in config.
    """
    auto_cfg = get_nested(config, "chunking.auto", {})
    min_headings = auto_cfg.get("min_headings", 3)
    max_gap = auto_cfg.get("max_heading_gap_levels", 2)
    min_section_tokens = auto_cfg.get("min_section_tokens", 100)
    max_section_tokens = auto_cfg.get("max_section_tokens", 2000)
    section_size_pass_ratio = auto_cfg.get("section_size_pass_ratio", 0.5)

    score = 0

    heading_count = _count_headings(markdown)
    if heading_count >= min_headings:
        score += 1

    if _check_heading_gaps(markdown, max_gap):
        score += 1

    if _check_heading_content_sizes(
        markdown,
        min_tokens=min_section_tokens,
        max_tokens=max_section_tokens,
        pass_ratio=section_size_pass_ratio,
    ):
        score += 1

    return score


# ---------------------------------------------------------------------------
# Auto chunker (format routing + structure scoring)
# ---------------------------------------------------------------------------

class AutoChunker(BaseChunker):
    """
    Automatic strategy selector.

    Decision flow:
      1. Check metadata["format"] against format_strategies config
      2. If format maps to a specific strategy → use it
      3. If format falls to "scoring" → compute structure score → heading or recursive
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._recursive = RecursiveChunker(config)
        self._heading = HeadingChunker(config)

        auto_cfg = get_nested(config, "chunking.auto", {})
        self._format_strategies = auto_cfg.get("format_strategies", {})
        self._image_formats = set(auto_cfg.get("image_formats", [
            "png", "jpg", "jpeg", "tiff", "bmp", "webp", "gif"
        ]))
        self._audio_formats = set(auto_cfg.get("audio_formats", [
            "mp3", "wav", "m4a", "flac", "aac", "ogg", "wma", "opus"
        ]))
        self._video_formats = set(auto_cfg.get("video_formats", [
            "mp4", "avi", "mkv", "webm", "mov", "wmv", "flv", "ts", "m4v"
        ]))
        self._threshold = auto_cfg.get("prefer_heading_threshold", 2)

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        """Auto-select strategy and chunk."""
        if not markdown.strip():
            return []

        strategy = self._select_strategy(markdown, metadata)

        if strategy == "heading":
            return self._heading.chunk(markdown, metadata)
        elif strategy == "whole":
            return [Chunk(
                text=markdown,
                metadata={**metadata, "chunk_index": 0, "total_chunks": 1,
                          "tokens": self.estimate_tokens(markdown)},
            )]
        elif strategy == "slide":
            try:
                from .slide import SlideChunker
                return SlideChunker(self.config).chunk(markdown, metadata)
            except ImportError:
                return self._recursive.chunk(markdown, metadata)
        elif strategy == "sheet":
            try:
                from .sheet import SheetChunker
                return SheetChunker(self.config).chunk(markdown, metadata)
            except ImportError:
                return self._recursive.chunk(markdown, metadata)
        elif strategy == "timestamp":
            try:
                from .timestamp import TimestampChunker
                return TimestampChunker(self.config).chunk(markdown, metadata)
            except ImportError:
                return self._recursive.chunk(markdown, metadata)
        else:
            # "recursive" or any unknown → recursive
            return self._recursive.chunk(markdown, metadata)

    def _select_strategy(self, markdown: str, metadata: dict[str, Any]) -> str:
        """
        Select chunking strategy based on format + structure.

        Returns strategy name: "heading", "recursive", "slide", "sheet", "whole", "timestamp".
        """
        doc_format = metadata.get("format", "").lower()

        # Check image formats
        if doc_format in self._image_formats:
            return self._format_strategies.get("image", "whole")

        # Check audio/video formats → timestamp chunker
        if doc_format in self._audio_formats:
            return self._format_strategies.get("audio", "timestamp")
        if doc_format in self._video_formats:
            return self._format_strategies.get("video", "timestamp")

        # Check format-specific strategy
        if doc_format in self._format_strategies:
            return self._format_strategies[doc_format]

        # Default strategy from config
        default_strategy = self._format_strategies.get("default", "scoring")

        if default_strategy != "scoring":
            return default_strategy

        # Structure scoring for text-based documents
        score = compute_structure_score(markdown, self.config)

        if score >= self._threshold:
            return "heading"
        else:
            return "recursive"


# ---------------------------------------------------------------------------
# Whole chunker
# ---------------------------------------------------------------------------

class WholeChunker(BaseChunker):
    """Keep the whole document as one chunk."""

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        if not markdown.strip():
            return []
        return [Chunk(
            text=markdown,
            metadata={
                **metadata,
                "chunk_index": 0,
                "total_chunks": 1,
                "tokens": self.estimate_tokens(markdown),
            },
        )]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_chunker(config: dict[str, Any]) -> BaseChunker:
    """
    Create chunker based on config.

    Args:
        config: Full config dict.

    Returns:
        Chunker instance.
    """
    strategy = get_nested(config, "chunking.strategy", "auto")

    if strategy == "auto":
        return AutoChunker(config)
    elif strategy == "heading":
        return HeadingChunker(config)
    elif strategy == "recursive":
        return RecursiveChunker(config)
    elif strategy == "slide":
        from .slide import SlideChunker
        return SlideChunker(config)
    elif strategy == "sheet":
        from .sheet import SheetChunker
        return SheetChunker(config)
    elif strategy == "timestamp":
        from .timestamp import TimestampChunker
        return TimestampChunker(config)
    elif strategy == "whole":
        return WholeChunker(config)
    else:
        # Unknown strategy → auto
        return AutoChunker(config)
