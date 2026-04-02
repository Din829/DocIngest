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

from typing import Any

from .base import BaseChunker, Chunk


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
          {"type": "protected", "text": "...", "lines": (start, end)}
          {"type": "normal", "text": "..."}
        """
        segments: list[dict] = []
        i = 0
        n = len(lines)

        while i < n:
            # Check if current line starts a protected span
            span = self._find_span_at(i, spans)

            if span is not None:
                start, end = span
                block_text = "\n".join(lines[start:end + 1])
                segments.append({
                    "type": "protected",
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
        """Build chunk texts from segments, respecting max_tokens."""
        chunks: list[str] = []
        current_parts: list[str] = []
        current_tokens = 0

        for seg in segments:
            seg_tokens = self.estimate_tokens(seg["text"])

            if seg["type"] == "protected":
                # Protected block: keep intact
                max_allowed = int(self._max_tokens * self._allowed_overflow)

                if seg_tokens <= max_allowed:
                    # Fits within overflow limit — try to append to current chunk
                    if current_tokens + seg_tokens > self._max_tokens and current_parts:
                        # Flush current chunk first
                        chunks.append("\n".join(current_parts))
                        current_parts = []
                        current_tokens = 0

                    current_parts.append(seg["text"])
                    current_tokens += seg_tokens
                else:
                    # Protected block exceeds even overflow limit — force as own chunk
                    if current_parts:
                        chunks.append("\n".join(current_parts))
                        current_parts = []
                        current_tokens = 0
                    chunks.append(seg["text"])
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

                    # Paragraph doesn't fit — flush current chunk
                    if current_parts:
                        chunks.append("\n\n".join(current_parts))
                        current_parts = []
                        current_tokens = 0

                    # Paragraph itself within limit
                    if para_tokens <= self._max_tokens:
                        current_parts.append(para)
                        current_tokens = para_tokens
                        continue

                    # Paragraph exceeds limit — split by sentences
                    sentence_chunks = self._split_paragraph(para)
                    for sc in sentence_chunks:
                        sc_tokens = self.estimate_tokens(sc)
                        if current_tokens + sc_tokens <= self._max_tokens:
                            current_parts.append(sc)
                            current_tokens += sc_tokens
                        else:
                            if current_parts:
                                chunks.append("\n\n".join(current_parts))
                            current_parts = [sc]
                            current_tokens = sc_tokens

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
            # Can't split by sentences — hard split by character count
            max_chars = self._max_tokens * 4  # ~4 chars per token
            result = []
            for i in range(0, len(text), max_chars):
                result.append(text[i:i + max_chars])
            return result

        return [p.strip() for p in parts if p.strip()]

    def _finalize(
        self, raw_chunks: list[str], metadata: dict[str, Any]
    ) -> list[Chunk]:
        """Convert raw text chunks to Chunk objects with metadata."""
        total = len(raw_chunks)
        result: list[Chunk] = []

        for i, text in enumerate(raw_chunks):
            chunk_meta = {
                **metadata,
                "chunk_index": i,
                "total_chunks": total,
                "tokens": self.estimate_tokens(text),
            }
            result.append(Chunk(text=text, metadata=chunk_meta))

        return result
