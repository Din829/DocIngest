"""
Base chunker interface + protection rules.

All chunkers implement this interface. The pipeline calls chunk() and gets
back a list of Chunk objects.

Protection rules (tables, code blocks, lists, quotes) are implemented here
in the base class so ALL chunkers automatically respect them.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """A single chunk of text with metadata."""

    # Chunk content (after enrichment, includes path injection if enabled)
    text: str

    # Metadata for this chunk
    metadata: dict[str, Any] = field(default_factory=dict)
    # Expected keys: source, original_file, format, title_path, page,
    #   chunk_index, total_chunks, tokens, language, has_table, has_image_ref,
    #   parent_id


# ---------------------------------------------------------------------------
# Protection rule: detect blocks that should not be split
# ---------------------------------------------------------------------------

# Patterns for protected blocks
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*```")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_QUOTE_RE = re.compile(r"^\s*>\s?")


def find_protected_spans(
    lines: list[str],
    protect_tables: bool = True,
    protect_code_blocks: bool = True,
    protect_lists: bool = True,
    protect_quotes: bool = True,
) -> list[tuple[int, int]]:
    """
    Find line ranges that should not be split.

    Returns list of (start_line, end_line) inclusive tuples.
    These spans represent tables, code blocks, lists, and quotes
    that should be kept as a single unit.

    Args:
        lines: Document split into lines.
        protect_*: Which block types to protect (from config).

    Returns:
        Sorted, non-overlapping list of (start, end) line indices.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(lines)

    while i < n:
        # Code blocks (``` ... ```)
        if protect_code_blocks and _CODE_FENCE_RE.match(lines[i]):
            start = i
            i += 1
            while i < n and not _CODE_FENCE_RE.match(lines[i]):
                i += 1
            end = min(i, n - 1)
            spans.append((start, end))
            i += 1
            continue

        # Tables (consecutive | rows)
        if protect_tables and _TABLE_ROW_RE.match(lines[i]):
            start = i
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                i += 1
            spans.append((start, i - 1))
            continue

        # Lists (consecutive - / 1. items, may have indented continuations)
        if protect_lists and _LIST_ITEM_RE.match(lines[i]):
            start = i
            i += 1
            while i < n and (
                _LIST_ITEM_RE.match(lines[i])
                or (lines[i].startswith("  ") and lines[i].strip())  # indented continuation
            ):
                i += 1
            spans.append((start, i - 1))
            continue

        # Quotes (consecutive > lines)
        if protect_quotes and _QUOTE_RE.match(lines[i]):
            start = i
            while i < n and _QUOTE_RE.match(lines[i]):
                i += 1
            spans.append((start, i - 1))
            continue

        i += 1

    return spans


def is_in_protected_span(
    line_idx: int,
    spans: list[tuple[int, int]],
) -> bool:
    """Check if a line index falls within any protected span."""
    for start, end in spans:
        if start <= line_idx <= end:
            return True
    return False


# ---------------------------------------------------------------------------
# Base Chunker
# ---------------------------------------------------------------------------

class BaseChunker(ABC):
    """
    Abstract base class for chunkers.

    Subclasses must implement:
      - chunk(markdown, metadata) → list[Chunk]

    Protection rules are available via find_protected_spans() utility.
    Subclasses should call it and respect the spans when splitting.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        # Shortcut to chunking config section
        self._chunking = config.get("chunking", {})
        self._max_tokens = self._chunking.get("max_tokens", 512)
        self._min_tokens = self._chunking.get("min_tokens", 100)
        self._overlap = self._chunking.get("overlap_tokens", 50)

        # Protection settings
        protection = self._chunking.get("protection", {})
        self._protect_tables = protection.get("tables", True)
        self._protect_code = protection.get("code_blocks", True)
        self._protect_lists = protection.get("lists", True)
        self._protect_quotes = protection.get("quotes", True)

        # allowed_overflow: supports both single number (legacy) and per-type dict.
        overflow_raw = protection.get("allowed_overflow", 2.0)
        if isinstance(overflow_raw, dict):
            self._overflow_map = overflow_raw
        else:
            # Single number → uniform for all types
            self._overflow_map = {"default": float(overflow_raw)}

        # on_overflow: what to do when a protected block EXCEEDS the allowed
        # overflow budget. Per-block-type so each format (table/code/list/
        # quote) can pick the right trade-off. Legacy configs without this
        # key get "bypass" everywhere, preserving pre-2026 behaviour.
        on_overflow_raw = protection.get("on_overflow", {})
        if isinstance(on_overflow_raw, str):
            # Single string → uniform
            self._on_overflow_map = {"default": on_overflow_raw}
        elif isinstance(on_overflow_raw, dict):
            self._on_overflow_map = on_overflow_raw
        else:
            self._on_overflow_map = {"default": "bypass"}

        # table_split parameters — only consulted when on_overflow == row_split.
        # Kept as BaseChunker attrs so any future chunker subclass inherits
        # the same knobs.
        table_split = protection.get("table_split", {}) or {}
        self._table_keep_header = bool(table_split.get("keep_header_in_every_chunk", True))
        self._table_max_rows = table_split.get("max_rows_per_chunk", None)
        if self._table_max_rows is not None:
            self._table_max_rows = int(self._table_max_rows)

    def _get_overflow(self, block_type: str = "default") -> float:
        """Get allowed overflow multiplier for a protected block type."""
        return float(self._overflow_map.get(
            block_type, self._overflow_map.get("default", 2.0)
        ))

    def _get_overflow_strategy(self, block_type: str = "default") -> str:
        """
        Get the on_overflow strategy (bypass | row_split | warn_and_bypass)
        for a protected block type. Falls back to 'default' key, then to
        'bypass' for maximum backwards compatibility.
        """
        return str(self._on_overflow_map.get(
            block_type, self._on_overflow_map.get("default", "bypass")
        ))

    @abstractmethod
    def chunk(
        self,
        markdown: str,
        metadata: dict[str, Any],
    ) -> list[Chunk]:
        """
        Split markdown text into chunks.

        Args:
            markdown: The full markdown content of a document.
            metadata: Document-level metadata (from ParseResult).
                Contains: source, original_file, format, language, etc.

        Returns:
            List of Chunk objects with text and per-chunk metadata.
        """
        ...

    def _get_protected_spans(self, lines: list[str]) -> list[tuple[int, int]]:
        """Convenience: get protected spans using this chunker's config."""
        return find_protected_spans(
            lines,
            protect_tables=self._protect_tables,
            protect_code_blocks=self._protect_code,
            protect_lists=self._protect_lists,
            protect_quotes=self._protect_quotes,
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Estimate token count from text.

        CJK-aware: CJK characters ≈ 1.5 tokens each, ASCII ≈ 0.25 tokens each.
        The naive len//4 underestimates CJK by 5-7x, breaking all chunk size logic.
        """
        cjk = 0
        for ch in text:
            cp = ord(ch)
            # CJK Unified Ideographs + common CJK ranges
            if (0x2E80 <= cp <= 0x9FFF    # CJK radicals, Kangxi, ideographs
                or 0xF900 <= cp <= 0xFAFF  # CJK compatibility
                or 0xFE30 <= cp <= 0xFE4F  # CJK compatibility forms
                or 0x3040 <= cp <= 0x30FF  # Hiragana + Katakana
                or 0xAC00 <= cp <= 0xD7AF  # Korean Hangul
                or 0x20000 <= cp <= 0x2FA1F):  # CJK extension B+
                cjk += 1
        ascii_chars = len(text) - cjk
        return max(1, int(cjk * 1.5 + ascii_chars * 0.25))
