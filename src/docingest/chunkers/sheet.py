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


def _count_cells(line: str) -> int:
    """
    Count data cells in a markdown table row.

    A row like `| a | b | c |` has 3 cells. Escaped pipes (`\\|`) inside cell
    content are not separators. Returns 0 for empty / non-row input so callers
    can use `_count_cells(x) > 1` safely without a type check.
    """
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return 0
    # Replace escaped pipes with a placeholder so they don't count as separators.
    s = s.replace(r"\|", "\x00")
    # A row of N cells has N+1 pipe characters; subtract 1 to get cell count.
    return max(0, s.count("|") - 1)


def _detect_caption_header(table_lines: list[str]) -> int:
    """
    Detect a leading "caption row + separator" preamble that should be carried
    along with the real column-header row when the table is split into chunks.

    Excel files frequently start a sheet with a one-cell title above the real
    column headers, e.g.

        | ■運用全般ルール・対応指針 |    ← caption (1 cell)
        | --- |                          ← separator
        |  | No | Category1 | ... |      ← real header (many cells)
        |  | 1 | メール | ... |           ← data
        |  | 2 | メール | ... |           ← data

    Markdown table chunkers that blindly treat the FIRST row as the header
    end up replicating only the caption into every chunk and dropping the
    real column names from chunks 1..N — meaning downstream RAG / LLM sees
    `| 22 | 運用 | ... |` without ever knowing the columns are
    `No / Category1 / Category2 / ...`.

    This helper looks at the first few rows and returns how many leading
    rows form the caption preamble. Returns 0 (no caption) for tables that
    look normal — keeping the caller's behaviour byte-identical for the
    common case.

    Returns:
        0  — no caption preamble; the first row IS the real header
        2  — first row is a caption + separator pair; the REAL header is row[2]
             (the caller treats rows 0..2 as the header_block to repeat)

    Heuristic (purely structural, no content sniffing — works for any xlsx
    whose caption isn't recognised by name):
      - row[0] has exactly 1 cell  (caption is a single-cell title)
      - row[1] is a markdown separator
      - row[2] exists and has > 1 cell  (real header has multiple columns)
      - the typical data row (sampled from row[3] onward) has a cell count
        within 2 of row[2]'s cell count  (real header ≈ data row width)
      - row[2] is itself a regular row, NOT another separator
    """
    if len(table_lines) < 4:
        # Need at least: caption + sep + real_header + at least 1 data row.
        return 0

    cap_cells = _count_cells(table_lines[0])
    if cap_cells != 1:
        return 0
    if not _TABLE_SEP_RE.match(table_lines[1]):
        return 0
    if _TABLE_SEP_RE.match(table_lines[2]):
        # Two separators in a row — not a caption preamble, just an odd table.
        return 0

    real_header_cells = _count_cells(table_lines[2])
    if real_header_cells <= 1:
        # Real header should be wider than the caption — otherwise we can't
        # tell it apart from a continuation of the caption.
        return 0

    # Sample up to 5 subsequent rows and check their cell-count agreement
    # with the would-be real header. A real header has roughly the same width
    # as its data rows; a caption usually does not.
    sample = table_lines[3:8]
    if not sample:
        return 0
    matches = sum(
        1 for ln in sample
        if abs(_count_cells(ln) - real_header_cells) <= 2
    )
    # Require majority agreement so a stray row doesn't trip the heuristic.
    if matches < max(1, len(sample) // 2):
        return 0

    return 2  # caption(0) + separator(1) — real header is at index 2


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

        When the table starts with a single-cell caption above the real column
        header (a common shape for xlsx sheets that have a title row above the
        column names — see `_detect_caption_header`), BOTH the caption row and
        the real header row are kept in the per-chunk header block. This way
        every chunk carries the column names, not just the caption — so an LLM
        reading chunk N can still tell which column means "No" vs "Category"
        vs "ルール" etc. For ordinary tables (no caption), behaviour is
        unchanged.
        """
        if len(table_lines) < 3:
            return None  # Need at least header + separator + 1 data row

        # Detect optional caption preamble. When present (offset == 2), the
        # real header sits at index 2 and the caption + separator at 0..1
        # need to be carried into every chunk too.
        caption_offset = _detect_caption_header(table_lines)
        if caption_offset == 2:
            header_lines = table_lines[0:3]  # caption + sep + real header
            data_rows = table_lines[3:]
        else:
            header_lines = table_lines[0:2]  # original behaviour
            data_rows = table_lines[2:]

        if not data_rows:
            return None

        header_block = "\n".join(header_lines)
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
