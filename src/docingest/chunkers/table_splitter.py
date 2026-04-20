"""
Row-level Markdown table splitter.

Used when a table (kept intact by the protection rule) exceeds the overflow
budget and we need to break it up without destroying its structure. The
industry-standard 2026 approach is to split at data-row boundaries and
repeat the header + separator row in every chunk so each piece remains
self-describing. See: Chonkie TableChunker, Ragie's table chunking blog,
Docling issue #2975 (header propagation bug this module works around).

Pure string manipulation. No regex magic numbers, no format assumptions
beyond "lines that start with `|` are table rows". Everything tunable is
passed as a parameter so callers (different chunkers / projects) can
override behaviour without editing this file.
"""

from __future__ import annotations

from typing import Callable


def _is_table_row(line: str) -> bool:
    """Markdown table rows start with an optional whitespace then `|`."""
    stripped = line.lstrip()
    return stripped.startswith("|")


def _looks_like_separator_row(line: str) -> bool:
    """
    Separator rows are composed of `|`, `:`, `-`, and whitespace only.
    Example:  `| :--- | :---: | ---: |`
    """
    if not _is_table_row(line):
        return False
    body = line.strip().strip("|")
    # Internal `|` stays in body (e.g. "| :--- | :--- |".strip("|") keeps
    # the middle pipes), so `|` must be in the allowed set too. The leading
    # `bool(body)` guard rejects the `||` degenerate case.
    return bool(body.strip()) and all(ch in " :-|" for ch in body)


def split_markdown_table(
    table_text: str,
    max_tokens: int,
    estimate_tokens: Callable[[str], int],
    keep_header_in_every_chunk: bool = True,
    max_rows_per_chunk: int | None = None,
) -> list[str]:
    """
    Split an oversized Markdown table into smaller self-describing pieces.

    Args:
        table_text: The full table Markdown (possibly with surrounding blank
            lines, which are preserved into the first chunk).
        max_tokens: Token budget per resulting chunk.
        estimate_tokens: Token-count function (typically BaseChunker.estimate_tokens).
            Injected so the splitter stays independent of the tokenizer.
        keep_header_in_every_chunk: When true, every chunk after the first
            re-emits the header and separator rows so downstream retrieval
            can read it standalone. False only makes sense when the caller
            keeps ordering guarantees.
        max_rows_per_chunk: Optional absolute row-count ceiling. None → only
            the token budget applies. Useful when a single row's tokens
            still fit but the caller prefers smaller chunks for retrieval
            granularity.

    Returns:
        A list of Markdown table strings. When the input already fits the
        budget (or cannot be split — no data rows) the original is returned
        as a single-element list.
    """
    if not table_text.strip():
        return [table_text]

    # Preserve leading/trailing blank lines: split off, re-attach to first/last
    # chunks. This keeps round-tripping stable when the surrounding chunker
    # joins segments with "\n".
    lines = table_text.split("\n")
    # Classify each line. We only treat the contiguous leading run of table
    # rows as "the table"; anything after a non-table line is treated as
    # trailing text (rare, but Docling occasionally emits stray captions).
    header_idx: int | None = None
    separator_idx: int | None = None
    data_start: int | None = None
    data_end: int | None = None  # exclusive

    for i, line in enumerate(lines):
        if _is_table_row(line):
            header_idx = i
            break
    if header_idx is None:
        return [table_text]

    # Header is the first row. Next row may or may not be a separator.
    if header_idx + 1 < len(lines) and _looks_like_separator_row(lines[header_idx + 1]):
        separator_idx = header_idx + 1
        data_start = separator_idx + 1
    else:
        separator_idx = None
        data_start = header_idx + 1

    # Find end of contiguous table rows
    data_end = data_start
    while data_end < len(lines) and _is_table_row(lines[data_end]):
        data_end += 1

    header_block_lines = lines[header_idx:data_start]  # header (+ separator)
    data_rows = lines[data_start:data_end]
    trailing = lines[data_end:]                         # anything after the table

    if not data_rows:
        # Header-only or unparseable table — don't fabricate splits.
        return [table_text]

    header_block = "\n".join(header_block_lines)
    header_tokens = estimate_tokens(header_block)

    # Edge case: header alone already exceeds the budget. We still split
    # data rows one-per-chunk to keep per-chunk size bounded; the header
    # repetition is unavoidable overhead.
    per_chunk_budget_for_rows = max(1, max_tokens - header_tokens) if keep_header_in_every_chunk else max_tokens

    chunks: list[list[str]] = []
    current_rows: list[str] = []
    current_tokens = 0

    def _flush():
        nonlocal current_rows, current_tokens
        if not current_rows:
            return
        if keep_header_in_every_chunk:
            chunk_lines = header_block_lines + current_rows
        else:
            chunk_lines = (header_block_lines if not chunks else []) + current_rows
        chunks.append(chunk_lines)
        current_rows = []
        current_tokens = 0

    for row in data_rows:
        row_tokens = estimate_tokens(row)
        would_exceed_tokens = (
            current_tokens + row_tokens > per_chunk_budget_for_rows and current_rows
        )
        would_exceed_rows = (
            max_rows_per_chunk is not None
            and len(current_rows) >= max_rows_per_chunk
        )
        if would_exceed_tokens or would_exceed_rows:
            _flush()
        current_rows.append(row)
        current_tokens += row_tokens
    _flush()

    if not chunks:
        # Degenerate — fall back to returning the whole table.
        return [table_text]

    # Attach leading blank lines (before header) to the first chunk and
    # trailing lines to the last chunk so caller-observable boundaries
    # don't shift.
    leading = lines[:header_idx]
    if leading:
        chunks[0] = leading + chunks[0]
    if trailing:
        chunks[-1] = chunks[-1] + trailing

    return ["\n".join(c) for c in chunks]
