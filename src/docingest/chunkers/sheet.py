"""
Sheet chunker — splits Excel/CSV converted Markdown by sheets and row groups.

Strategy:
  - Multi-sheet: split by sheet name heading first
  - Within each sheet: split into row groups fitting max_tokens
  - Header row repeated at the top of each chunk (context preservation)

Key design:
  - Detects Markdown table headers (| Header1 | Header2 |)
  - Repeats header + separator in each chunk so rows have context
  - Falls back to recursive if no table structure detected
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker


_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[-:| ]+\|\s*$")


class SheetChunker(BaseChunker):
    """Split Excel/CSV Markdown by sheets and row groups with header repetition."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._recursive = RecursiveChunker(config)

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        """Split by sheets, then row groups."""
        if not markdown.strip():
            return []

        # Try to find table structure
        sheets = self._split_sheets(markdown)

        all_chunks: list[Chunk] = []

        for sheet_name, sheet_text in sheets:
            sheet_meta = {**metadata}
            if sheet_name:
                sheet_meta["sheet_name"] = sheet_name

            table_chunks = self._chunk_table(sheet_text, sheet_meta)

            if table_chunks:
                all_chunks.extend(table_chunks)
            else:
                # No table structure → recursive fallback
                all_chunks.extend(self._recursive.chunk(sheet_text, sheet_meta))

        # Renumber
        for i, c in enumerate(all_chunks):
            c.metadata["chunk_index"] = i
            c.metadata["total_chunks"] = len(all_chunks)
            c.metadata["tokens"] = self.estimate_tokens(c.text)

        return all_chunks

    def _split_sheets(self, markdown: str) -> list[tuple[str, str]]:
        """
        Split by sheet name headings (e.g., "## Sheet1", "## 売上データ").

        Returns list of (sheet_name, content) tuples.
        If no sheet headings found, returns [("", full_text)].
        """
        parts = re.split(r"^(#{1,2}\s+.+)$", markdown, flags=re.MULTILINE)

        if len(parts) <= 1:
            # No headings → single sheet
            return [("", markdown)]

        sheets: list[tuple[str, str]] = []

        # parts[0] is content before first heading (may be empty)
        if parts[0].strip():
            sheets.append(("", parts[0]))

        # Iterate heading + content pairs
        for i in range(1, len(parts), 2):
            heading = parts[i].strip().lstrip("#").strip()
            content = parts[i + 1] if i + 1 < len(parts) else ""
            sheets.append((heading, content))

        return sheets

    def _chunk_table(
        self, text: str, metadata: dict[str, Any]
    ) -> list[Chunk] | None:
        """
        Split a Markdown table into row-group chunks with header repetition.

        Returns None if no table structure found (caller should use recursive).
        """
        lines = text.split("\n")

        # Find header + separator
        header_line: str | None = None
        separator_line: str | None = None
        data_start: int = 0
        preamble_lines: list[str] = []

        for i, line in enumerate(lines):
            if _TABLE_SEP_RE.match(line) and i > 0 and _TABLE_ROW_RE.match(lines[i - 1]):
                header_line = lines[i - 1]
                separator_line = line
                data_start = i + 1
                preamble_lines = lines[:i - 1]
                break

        if header_line is None:
            return None  # No table found

        # Collect data rows
        data_rows: list[str] = []
        postamble_lines: list[str] = []
        in_table = True

        for i in range(data_start, len(lines)):
            if in_table and _TABLE_ROW_RE.match(lines[i]):
                data_rows.append(lines[i])
            else:
                in_table = False
                postamble_lines.append(lines[i])

        if not data_rows:
            return None

        # Build header block (repeated in each chunk)
        header_block = f"{header_line}\n{separator_line}"
        header_tokens = self.estimate_tokens(header_block)
        preamble_text = "\n".join(preamble_lines).strip()
        preamble_tokens = self.estimate_tokens(preamble_text) if preamble_text else 0

        # Split data rows into groups that fit max_tokens
        budget = self._max_tokens - header_tokens - preamble_tokens
        if budget < 50:
            budget = 200  # Minimum budget for data

        chunks: list[Chunk] = []
        current_rows: list[str] = []
        current_tokens = 0

        for row in data_rows:
            row_tokens = self.estimate_tokens(row)

            if current_tokens + row_tokens > budget and current_rows:
                # Flush current group
                chunk_text = self._build_table_chunk(
                    preamble_text, header_block, current_rows
                )
                chunks.append(Chunk(text=chunk_text, metadata={**metadata}))
                current_rows = []
                current_tokens = 0

            current_rows.append(row)
            current_tokens += row_tokens

        # Flush remaining
        if current_rows:
            chunk_text = self._build_table_chunk(
                preamble_text, header_block, current_rows
            )
            chunks.append(Chunk(text=chunk_text, metadata={**metadata}))

        # Append postamble to last chunk if any
        postamble = "\n".join(postamble_lines).strip()
        if postamble and chunks:
            chunks[-1] = Chunk(
                text=chunks[-1].text + "\n\n" + postamble,
                metadata=chunks[-1].metadata,
            )

        return chunks

    def _build_table_chunk(
        self, preamble: str, header_block: str, rows: list[str]
    ) -> str:
        """Build a chunk with optional preamble + header + data rows."""
        parts: list[str] = []
        if preamble:
            parts.append(preamble)
            parts.append("")
        parts.append(header_block)
        parts.extend(rows)
        return "\n".join(parts)
