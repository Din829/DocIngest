"""
Recursive character chunker — the universal default strategy.

Splits text respecting natural boundaries in priority order:
  1. Paragraph boundary (blank line)
  2. Sentence boundary (。. ! ? + newline)
  3. Mid-sentence (last resort, only when max_tokens reached)

Protection rules (from base.py) are applied: tables, code blocks,
lists, and quotes are kept intact as single units.

Based on: Vecta 2026.02 benchmark — recursive 512t scored 69% (highest).
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseChunker, Chunk
from .table_splitter import split_markdown_table

logger = logging.getLogger(__name__)


class RecursiveChunker(BaseChunker):
    """Recursive character splitting with protection rules."""

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        """Split markdown into chunks respecting boundaries and protection."""
        if not markdown.strip():
            return []

        lines = markdown.split("\n")
        protected_spans = self._get_protected_spans(lines)

        # Split into segments: protected blocks + normal text
        segments = self._split_into_segments(lines, protected_spans)

        # Build chunks from segments
        raw_chunks = self._build_chunks(segments)

        # Convert to Chunk objects with metadata
        return self._finalize(raw_chunks, metadata)

    def _split_into_segments(
        self,
        lines: list[str],
        spans: list[tuple[int, int]],
    ) -> list[dict]:
        """
        Split lines into segments, separating protected blocks from normal text.

        Each segment is either:
          {"type": "protected", "block_type": "table|list|...", "text": "...", "lines": (start, end)}
          {"type": "normal", "text": "..."}
        """
        from .base import _TABLE_ROW_RE, _CODE_FENCE_RE, _LIST_ITEM_RE, _QUOTE_RE

        segments: list[dict] = []
        i = 0
        n = len(lines)

        while i < n:
            # Check if current line starts a protected span
            span = self._find_span_at(i, spans)

            if span is not None:
                start, end = span
                block_text = "\n".join(lines[start:end + 1])
                # Detect block type from first line
                first_line = lines[start]
                if _CODE_FENCE_RE.match(first_line):
                    block_type = "code_block"
                elif _TABLE_ROW_RE.match(first_line):
                    block_type = "table"
                elif _LIST_ITEM_RE.match(first_line):
                    block_type = "list"
                elif _QUOTE_RE.match(first_line):
                    block_type = "quote"
                else:
                    block_type = "default"
                segments.append({
                    "type": "protected",
                    "block_type": block_type,
                    "text": block_text,
                    "lines": (start, end),
                })
                i = end + 1
            else:
                # Collect normal lines until next protected span or end
                normal_start = i
                while i < n and self._find_span_at(i, spans) is None:
                    i += 1
                normal_text = "\n".join(lines[normal_start:i])
                if normal_text.strip():
                    segments.append({"type": "normal", "text": normal_text})

        return segments

    def _find_span_at(
        self, line_idx: int, spans: list[tuple[int, int]]
    ) -> tuple[int, int] | None:
        """Find if a protected span starts at this line index."""
        for start, end in spans:
            if start == line_idx:
                return (start, end)
        return None

    def _build_chunks(self, segments: list[dict]) -> list[str]:
        """Build chunk texts from segments, respecting max_tokens + overlap."""
        chunks = self._split_segments(segments)

        # Apply overlap: prepend tail of previous chunk to next chunk
        if self._overlap > 0 and len(chunks) > 1:
            chunks = self._apply_overlap(chunks)

        return chunks

    def _apply_overlap(self, chunks: list[str]) -> list[str]:
        """Add overlap tokens from the end of each chunk to the start of the next."""
        result = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = chunks[i - 1]
            # Extract tail ~overlap tokens from previous chunk
            overlap_text = self._extract_tail(prev, self._overlap)
            if overlap_text:
                result.append(overlap_text + "\n\n" + chunks[i])
            else:
                result.append(chunks[i])
        return result

    @staticmethod
    def _extract_tail(text: str, target_tokens: int) -> str:
        """Extract approximately target_tokens worth of text from the end."""
        # Work backwards by sentences/paragraphs
        parts = text.split("\n\n")
        tail_parts: list[str] = []
        tail_tokens = 0
        for part in reversed(parts):
            from .base import BaseChunker
            part_tokens = BaseChunker.estimate_tokens(part)
            if tail_tokens + part_tokens > target_tokens and tail_parts:
                break
            tail_parts.insert(0, part)
            tail_tokens += part_tokens
            if tail_tokens >= target_tokens:
                break
        return "\n\n".join(tail_parts) if tail_parts else ""

    def _split_segments(self, segments: list[dict]) -> list[str]:
        """Split segments into raw chunk texts (no overlap yet).

        Respects min_tokens: avoids flushing tiny chunks by allowing
        the accumulator to moderately exceed max_tokens rather than
        emitting a fragment below min_tokens.
        """
        chunks: list[str] = []
        current_parts: list[str] = []
        current_tokens = 0
        # Hard ceiling to prevent runaway accumulation while enforcing
        # min_tokens. Reuses protection.allowed_overflow.default so the
        # same knob controls both "protected block may exceed by X" and
        # "normal-text accumulator may exceed by X" — one config value,
        # consistent behaviour, no new field to learn.
        merge_ceiling = int(self._max_tokens * self._get_overflow("default"))

        for seg in segments:
            seg_tokens = self.estimate_tokens(seg["text"])

            if seg["type"] == "protected":
                # Protected block: keep intact, use per-type overflow limit
                block_type = seg.get("block_type", "default")
                max_allowed = int(self._max_tokens * self._get_overflow(block_type))

                if seg_tokens <= max_allowed:
                    # Fits within overflow limit — try to append to current chunk
                    # BUT: don't flush a tiny current chunk (below min_tokens)
                    # just to start a new one. This prevents fragments like
                    # a lone "### Section Title" immediately followed by a
                    # large table from being split into (heading-alone) +
                    # (table), when clearly they belong together.
                    #
                    # The ceiling here is max_allowed (the per-type overflow
                    # budget), not merge_ceiling (1.5× max_tokens for normal
                    # text). Protected blocks are already permitted to exceed
                    # max_tokens by their overflow factor, so we allow the
                    # combined chunk up to that same limit — otherwise the
                    # small-fragment guard would be defeated for any table
                    # larger than ~1.5× max_tokens.
                    combined = current_tokens + seg_tokens
                    should_flush = (
                        combined > self._max_tokens
                        and current_parts
                        and (
                            current_tokens >= self._min_tokens
                            or combined > max_allowed
                        )
                    )
                    if should_flush:
                        chunks.append("\n".join(current_parts))
                        current_parts = []
                        current_tokens = 0

                    current_parts.append(seg["text"])
                    current_tokens += seg_tokens
                else:
                    # Protected block exceeds even overflow limit. The previous
                    # default was to emit the block as-is, but that allowed
                    # pathological Markdown tables (e.g. Docling expands merged
                    # cells into multi-kilo-token tables) to flow downstream
                    # unchanged — see problem A in tests. Behaviour is now
                    # config-driven per block type:
                    #   bypass           — old behaviour (may be desirable for
                    #                      code blocks where mid-split breaks
                    #                      syntax)
                    #   row_split        — for tables: split at data-row
                    #                      boundaries, repeat header in every
                    #                      chunk (2026 industry standard)
                    #   warn_and_bypass  — same as bypass + logs a warning so
                    #                      oversized blocks stay discoverable
                    strategy = self._get_overflow_strategy(block_type)

                    # Decide prefix vs flush: a small current_parts (below
                    # min_tokens) is almost always a section heading or
                    # similar context that should stay glued to the
                    # following block. Prepend it to the first emitted
                    # sub-chunk rather than emitting it alone.
                    prefix = ""
                    if current_parts:
                        if current_tokens < self._min_tokens:
                            prefix = "\n".join(current_parts) + "\n"
                        else:
                            chunks.append("\n".join(current_parts))
                        current_parts = []
                        current_tokens = 0

                    if strategy == "row_split" and block_type == "table":
                        table_chunks = split_markdown_table(
                            seg["text"],
                            max_tokens=self._max_tokens,
                            estimate_tokens=self.estimate_tokens,
                            keep_header_in_every_chunk=self._table_keep_header,
                            max_rows_per_chunk=self._table_max_rows,
                        )
                        # Any sub-chunk that itself still exceeds max_tokens
                        # (one very wide row) falls back to bypass so we never
                        # hard-truncate inside a row — better to have one
                        # oversized row than corrupted content.
                        if prefix and table_chunks:
                            table_chunks[0] = prefix + table_chunks[0]
                        chunks.extend(table_chunks)
                    else:
                        if strategy == "warn_and_bypass":
                            logger.warning(
                                f"Oversized protected block ({block_type}, "
                                f"{seg_tokens} tokens > {max_allowed} allowed) "
                                f"emitted as a single chunk (on_overflow=bypass)."
                            )
                        chunks.append(prefix + seg["text"])
            else:
                # Normal text: split by paragraphs, then sentences
                paragraphs = seg["text"].split("\n\n")

                for para in paragraphs:
                    para = para.strip()
                    if not para:
                        continue

                    para_tokens = self.estimate_tokens(para)

                    # Paragraph fits in current chunk
                    if current_tokens + para_tokens <= self._max_tokens:
                        current_parts.append(para)
                        current_tokens += para_tokens
                        continue

                    # Paragraph doesn't fit — but don't flush if current chunk
                    # is too small (below min_tokens) and we haven't hit the ceiling
                    if current_parts and (
                        current_tokens >= self._min_tokens
                        or current_tokens + para_tokens > merge_ceiling
                    ):
                        chunks.append("\n\n".join(current_parts))
                        current_parts = []
                        current_tokens = 0

                    # Paragraph itself within limit
                    if para_tokens <= self._max_tokens:
                        current_parts.append(para)
                        current_tokens += para_tokens
                        continue

                    # Paragraph exceeds limit — split by sentences
                    sentence_chunks = self._split_paragraph(para)
                    for sc in sentence_chunks:
                        sc_tokens = self.estimate_tokens(sc)
                        if current_tokens + sc_tokens <= self._max_tokens:
                            current_parts.append(sc)
                            current_tokens += sc_tokens
                        else:
                            if current_parts and (
                                current_tokens >= self._min_tokens
                                or current_tokens + sc_tokens > merge_ceiling
                            ):
                                chunks.append("\n\n".join(current_parts))
                                current_parts = []
                                current_tokens = 0
                            current_parts.append(sc)
                            current_tokens += sc_tokens

        # Flush remaining
        if current_parts:
            chunks.append("\n\n".join(current_parts))

        return chunks

    def _split_paragraph(self, text: str) -> list[str]:
        """
        Split a long paragraph into sentence-level pieces.

        Boundary priority:
          1. Sentence-ending punctuation (。.!? followed by space/newline)
          2. Mid-text at max_tokens (last resort)
        """
        # Sentence-ending patterns
        import re
        # Split on sentence boundaries: period/。/!/? followed by space or end
        parts = re.split(r'(?<=[。．.!！?？])\s+', text)

        if len(parts) <= 1:
            # Can't split by sentences — fall back to character-level cut
            # sized by the actual tokenizer (CJK-aware estimate_tokens),
            # not a fixed "4 chars/token" multiplier. The old multiplier
            # under-cut CJK text ~6× because CJK chars are ~1.5 tok each,
            # not 0.25 like ASCII.
            return self._char_split_to_budget(text)

        return [p.strip() for p in parts if p.strip()]

    def _char_split_to_budget(self, text: str) -> list[str]:
        """
        Slice `text` into pieces each ≤ max_tokens according to
        estimate_tokens. Uses binary search to find the largest
        prefix that fits, advancing until the whole string is consumed.

        No mid-sentence heuristics here — this is the last-resort path
        taken only when there are no sentence boundaries to respect.
        Cost is O(n log n) on character count, which is cheap for the
        small fraction of inputs that reach this branch.
        """
        result: list[str] = []
        remaining = text
        budget = self._max_tokens
        # Defensive: if budget is somehow <= 0, avoid infinite loop.
        if budget <= 0 or not remaining:
            return [remaining] if remaining else []

        while remaining:
            # If the whole remainder fits, we're done in one piece.
            if self.estimate_tokens(remaining) <= budget:
                result.append(remaining)
                break

            # Binary search: largest `cut` where estimate_tokens(prefix) ≤ budget
            lo, hi = 1, len(remaining)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self.estimate_tokens(remaining[:mid]) <= budget:
                    lo = mid
                else:
                    hi = mid - 1

            # lo is now the maximal fitting cut. Guarantee forward progress:
            # a single character always fits (token estimation has a floor).
            cut = max(lo, 1)
            result.append(remaining[:cut])
            remaining = remaining[cut:]

        return result

    def _finalize(
        self, raw_chunks: list[str], metadata: dict[str, Any]
    ) -> list[Chunk]:
        """
        Convert raw text chunks to Chunk objects with metadata.

        Also deduplicates byte-identical adjacent chunks. This comes up when
        the source Markdown itself contains repeated blocks — for example
        Docling expands merged table cells by emitting the same data row
        multiple times, and row-level table splitting then produces several
        byte-identical sub-chunks. Keeping duplicates would waste embedding
        cost and skew retrieval weights. We only drop *adjacent* duplicates
        so that genuinely repeated content later in the document (rare, but
        possible) is preserved.
        """
        deduped: list[str] = []
        for text in raw_chunks:
            if deduped and deduped[-1] == text:
                continue
            deduped.append(text)

        total = len(deduped)
        result: list[Chunk] = []

        for i, text in enumerate(deduped):
            chunk_meta = {
                **metadata,
                "chunk_index": i,
                "total_chunks": total,
                "tokens": self.estimate_tokens(text),
            }
            result.append(Chunk(text=text, metadata=chunk_meta))

        return result
