"""
Sheet chunker — splits Excel/CSV converted Markdown by sheets and row groups.

Strategy (priority order for sheet splitting):
  1. Pagebreak markers (<!-- pagebreak -->) — Docling's native sheet separator
  2. Markdown headings (## SheetName) — fallback for older Docling / manual MD
  3. Treat as single sheet — final fallback

Within each sheet:
  - Detect multiple tables (each has its own header+separator)
  - Split each table into row groups fitting max_tokens
  - Repeat header row at the top of each chunk (context preservation)
  - Non-table text between tables → recursive chunker
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker
from ..parsers.base import PAGEBREAK_MARKER


_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[-:| ]+\|\s*$")


class SheetChunker(BaseChunker):
    """Split Excel/CSV Markdown by sheets and row groups with header repetition."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._recursive = RecursiveChunker(config)

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        """Split by sheets, then tables within each sheet."""
        if not markdown.strip():
            return []

        sheets = self._split_sheets(markdown)

        all_chunks: list[Chunk] = []

        for sheet_name, sheet_text in sheets:
            if not sheet_text.strip():
                continue

            sheet_meta = {**metadata}
            if sheet_name:
                sheet_meta["sheet_name"] = sheet_name
                sheet_meta["title_path"] = sheet_name

            # Process all content blocks (tables + non-table text) within sheet
            sheet_chunks = self._chunk_sheet_content(sheet_text, sheet_meta)
            all_chunks.extend(sheet_chunks)

        # Renumber
        for i, c in enumerate(all_chunks):
            c.metadata["chunk_index"] = i
            c.metadata["total_chunks"] = len(all_chunks)
            c.metadata["tokens"] = self.estimate_tokens(c.text)

        return all_chunks

    def _split_sheets(self, markdown: str) -> list[tuple[str, str]]:
        """
        Split into sheets using the best available separator.

        Priority:
          1. Pagebreak markers (Docling native, works for Excel/PDF/PPT)
          2. Markdown headings (## SheetName)
          3. Whole text as single sheet
        """
        # Priority 1: Pagebreak markers
        if PAGEBREAK_MARKER in markdown:
            sections = markdown.split(PAGEBREAK_MARKER)
            sheets: list[tuple[str, str]] = []
            for i, section in enumerate(sections):
                text = section.strip()
                if text:
                    # Try to extract a name from first heading in section
                    name = self._extract_first_heading(text) or f"Sheet {i + 1}"
                    sheets.append((name, text))
            if sheets:
                return sheets

        # Priority 2: Markdown headings
        parts = re.split(r"^(#{1,2}\s+.+)$", markdown, flags=re.MULTILINE)
        if len(parts) > 1:
            sheets = []
            if parts[0].strip():
                sheets.append(("", parts[0]))
            for i in range(1, len(parts), 2):
                heading = parts[i].strip().lstrip("#").strip()
                content = parts[i + 1] if i + 1 < len(parts) else ""
                sheets.append((heading, content))
            return sheets

        # Priority 3: Single sheet
        return [("", markdown)]

    @staticmethod
    def _extract_first_heading(text: str) -> str:
        """Extract the first heading from a text block, if any."""
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return ""

    def _chunk_sheet_content(
        self, text: str, metadata: dict[str, Any]
    ) -> list[Chunk]:
        """
        Process a single sheet: find all tables and non-table segments.

        A sheet may contain multiple tables with non-table text between them.
        Each table is chunked with header repetition.
        Non-table text goes through recursive chunker.
        """
        lines = text.split("\n")
        segments = self._segment_content(lines)
        chunks: list[Chunk] = []

        for seg_type, seg_lines in segments:
            seg_text = "\n".join(seg_lines).strip()
            if not seg_text:
                continue

            if seg_type == "table":
                table_chunks = self._chunk_single_table(seg_lines, metadata)
                if table_chunks:
                    chunks.extend(table_chunks)
                else:
                    # Table detection failed → recursive fallback
                    chunks.extend(self._recursive.chunk(seg_text, metadata))
            else:
                # Non-table text
                if self.estimate_tokens(seg_text) > 0:
                    chunks.extend(self._recursive.chunk(seg_text, metadata))

        return chunks

    def _segment_content(
        self, lines: list[str]
    ) -> list[tuple[str, list[str]]]:
        """
        Split lines into segments: ("table", [...]) or ("text", [...]).

        A "table" segment starts at a header row (line before a separator)
        and continues while lines match table row pattern.
        Everything else is "text".
        """
        segments: list[tuple[str, list[str]]] = []
        i = 0
        n = len(lines)
        current_text: list[str] = []

        while i < n:
            # Check if this line starts a table (next line is separator)
            if (i + 1 < n
                    and _TABLE_ROW_RE.match(lines[i])
                    and _TABLE_SEP_RE.match(lines[i + 1])):
                # Flush accumulated text
                if current_text:
                    segments.append(("text", current_text))
                    current_text = []

                # Collect table: header + separator + data rows
                table_lines = [lines[i], lines[i + 1]]
                i += 2
                while i < n and _TABLE_ROW_RE.match(lines[i]):
                    table_lines.append(lines[i])
                    i += 1

                segments.append(("table", table_lines))
            else:
                current_text.append(lines[i])
                i += 1

        # Flush remaining text
        if current_text:
            segments.append(("text", current_text))

        return segments

    def _chunk_single_table(
        self, table_lines: list[str], metadata: dict[str, Any]
    ) -> list[Chunk] | None:
        """
        Chunk a single table (header + separator + data rows) into row groups.

        Header is repeated at the top of each chunk.
        """
        if len(table_lines) < 3:
            return None  # Need at least header + separator + 1 data row

        header_line = table_lines[0]
        separator_line = table_lines[1]
        data_rows = table_lines[2:]

        if not data_rows:
            return None

        header_block = f"{header_line}\n{separator_line}"
        header_tokens = self.estimate_tokens(header_block)

        # Budget for data per chunk
        budget = self._max_tokens - header_tokens
        if budget < 50:
            budget = 200

        chunks: list[Chunk] = []
        current_rows: list[str] = []
        current_tokens = 0

        for row in data_rows:
            row_tokens = self.estimate_tokens(row)

            if current_tokens + row_tokens > budget and current_rows:
                chunk_text = header_block + "\n" + "\n".join(current_rows)
                chunks.append(Chunk(text=chunk_text, metadata={**metadata}))
                current_rows = []
                current_tokens = 0

            current_rows.append(row)
            current_tokens += row_tokens

        # Flush remaining
        if current_rows:
            chunk_text = header_block + "\n" + "\n".join(current_rows)
            chunks.append(Chunk(text=chunk_text, metadata={**metadata}))

        return chunks
