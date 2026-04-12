"""
Timestamp chunker — splits audio/video transcripts by time markers.

Designed for transcripts that contain `[MM:SS]` or `[HH:MM:SS]` markers
(produced by media_parser from SRT subtitles or ASR output).

Strategy:
  1. Find all [MM:SS] / [HH:MM:SS] markers in the Markdown.
  2. Group consecutive timestamped lines into chunks that fit within
     max_tokens.
  3. Each chunk carries start_time / end_time in metadata (for RAG
     queries like "what was said at minute 12?").
  4. Non-timestamped text (headers, metadata) is prepended to the first
     chunk as context.
  5. If no timestamps found → fall back to recursive chunker.

This mirrors the slide chunker pattern: format-specific semantic
boundaries (timestamps ↔ slide breaks), with recursive as the fallback.
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker


# Matches [MM:SS] or [HH:MM:SS] at line start
_TIMESTAMP_RE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*(.+)", re.MULTILINE)


def _parse_seconds(match: re.Match) -> int:
    """Convert a timestamp regex match to total seconds."""
    groups = match.groups()
    if groups[2] is not None:
        # HH:MM:SS
        return int(groups[0]) * 3600 + int(groups[1]) * 60 + int(groups[2])
    else:
        # MM:SS
        return int(groups[0]) * 60 + int(groups[1])


def _format_time(seconds: int) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class TimestampChunker(BaseChunker):
    """Split audio/video transcript by timestamp boundaries."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._recursive = RecursiveChunker(config)

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        """Split by timestamps, fallback to recursive if none found."""
        if not markdown.strip():
            return []

        # Extract timestamped entries
        entries = self._extract_entries(markdown)

        if not entries:
            # No timestamps → recursive fallback
            return self._recursive.chunk(markdown, metadata)

        # Separate header (non-timestamped) from transcript body
        header = self._extract_header(markdown)

        # Group entries into chunks that fit max_tokens
        chunks = self._build_chunks(entries, header, metadata)

        # Renumber
        for i, c in enumerate(chunks):
            c.metadata["chunk_index"] = i
            c.metadata["total_chunks"] = len(chunks)
            c.metadata["tokens"] = self.estimate_tokens(c.text)

        return chunks

    def _extract_entries(self, markdown: str) -> list[dict[str, Any]]:
        """Extract timestamped entries from markdown."""
        entries: list[dict[str, Any]] = []
        for match in _TIMESTAMP_RE.finditer(markdown):
            seconds = _parse_seconds(match)
            text = match.group(4).strip() if match.group(4) else match.group(3) or ""
            # Re-check: group(4) is the text after timestamp when 3 groups (HH:MM:SS)
            # or the text after timestamp when 2 groups (MM:SS)
            # The regex already captures the text in group(4)
            entries.append({
                "seconds": seconds,
                "text": text,
                "line": match.group(0),  # full original line
            })
        return entries

    def _extract_header(self, markdown: str) -> str:
        """
        Extract non-timestamped content before the first timestamp.
        This is typically the title, metadata, ## Transcript heading, etc.
        """
        first_match = _TIMESTAMP_RE.search(markdown)
        if not first_match:
            return ""
        header = markdown[:first_match.start()].strip()
        return header

    def _build_chunks(
        self,
        entries: list[dict[str, Any]],
        header: str,
        metadata: dict[str, Any],
    ) -> list[Chunk]:
        """Group timestamped entries into chunks respecting max_tokens."""
        chunks: list[Chunk] = []
        current_lines: list[str] = []
        current_tokens = 0
        chunk_start_sec: int | None = None
        chunk_end_sec: int = 0

        for entry in entries:
            line = entry["line"]
            line_tokens = self.estimate_tokens(line)

            if chunk_start_sec is None:
                chunk_start_sec = entry["seconds"]

            # Check if adding this line exceeds max_tokens
            if current_tokens + line_tokens > self._max_tokens and current_lines:
                # Flush current chunk
                text = "\n\n".join(current_lines)
                if header and not chunks:
                    # Prepend header to first chunk only
                    text = header + "\n\n" + text

                chunk_meta = {
                    **metadata,
                    "start_time": _format_time(chunk_start_sec or 0),
                    "end_time": _format_time(chunk_end_sec),
                    "start_seconds": chunk_start_sec or 0,
                    "end_seconds": chunk_end_sec,
                }
                chunks.append(Chunk(text=text, metadata=chunk_meta))

                # Start new chunk
                current_lines = []
                current_tokens = 0
                chunk_start_sec = entry["seconds"]

            current_lines.append(line)
            current_tokens += line_tokens
            chunk_end_sec = entry["seconds"]

        # Flush remaining
        if current_lines:
            text = "\n\n".join(current_lines)
            if header and not chunks:
                text = header + "\n\n" + text

            chunk_meta = {
                **metadata,
                "start_time": _format_time(chunk_start_sec or 0),
                "end_time": _format_time(chunk_end_sec),
                "start_seconds": chunk_start_sec or 0,
                "end_seconds": chunk_end_sec,
            }
            chunks.append(Chunk(text=text, metadata=chunk_meta))

        return chunks
