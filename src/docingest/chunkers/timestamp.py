"""
Timestamp chunker — slices audio/video transcripts at `[MM:SS]` markers,
then delegates the actual splitting to RecursiveChunker.

Strategy:
  1. Find every `[MM:SS]` / `[HH:MM:SS]` position in the markdown.
  2. Carve the body into segments — each segment is the text from one
     timestamp up to (but not including) the next.
  3. Merge tiny adjacent segments to avoid fragment chunks (below
     min_tokens). Two segments fuse only when the merged size still fits
     within max_tokens × overflow factor.
  4. Per segment, hand off to RecursiveChunker — it owns paragraph /
     sentence / character-level splitting, list-protection, min/max
     enforcement. This chunker is just a time-aware slicer + tagger.
  5. Stamp every emitted chunk with start_seconds / end_seconds derived
     from the owning segment.

Why delegate instead of reimplementing splits:
  RecursiveChunker already handles oversized segments via paragraph →
  sentence → character-level fallback, plus protection for tables / code /
  lists. Reimplementing that here drifted apart in the past (the original
  TimestampChunker silently dropped any line between `[MM:SS]` and the
  next timestamp that wasn't on the timestamp line itself). Delegation
  guarantees one source of truth.

Fallback: no timestamps found → pass the whole markdown to RecursiveChunker.
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker


# Find each timestamp's POSITION. We intentionally do NOT capture the text
# after the marker — the slice between two TS positions is the segment body,
# regardless of how the upstream prompt formatted it.
_TS_POS_RE = re.compile(
    r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]",
    re.MULTILINE,
)


def _ts_to_seconds(match: re.Match) -> int:
    a, b, c = match.group(1), match.group(2), match.group(3)
    if c is not None:
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


def _format_time(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class TimestampChunker(BaseChunker):
    """Slice transcripts at `[MM:SS]` boundaries, delegate splitting to recursive."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._recursive = RecursiveChunker(config)

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        if not markdown.strip():
            return []

        matches = list(_TS_POS_RE.finditer(markdown))
        if not matches:
            return self._recursive.chunk(markdown, metadata)

        header = markdown[: matches[0].start()].rstrip()

        # Each segment carries (start_sec, end_sec, text). text starts AT the
        # [MM:SS] line so the chunk keeps the marker for readability.
        segments: list[tuple[int, int, str]] = []
        for i, m in enumerate(matches):
            start_sec = _ts_to_seconds(m)
            next_start = (
                matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            )
            text = markdown[m.start():next_start].rstrip()
            segments.append((start_sec, start_sec, text))  # end filled below
        # end_sec = next segment's start_sec (or own start if last)
        segments = [
            (s, segments[i + 1][0] if i + 1 < len(segments) else s, t)
            for i, (s, _, t) in enumerate(segments)
        ]

        # Merge tiny adjacent segments up to a soft ceiling so we don't emit
        # fragment chunks. Ceiling reuses max_tokens × default overflow — the
        # same knob RecursiveChunker uses internally, so behaviour is
        # consistent across both chunkers.
        merge_ceiling = int(self._max_tokens * self._get_overflow("default"))
        merged: list[tuple[int, int, str]] = []
        for start, end, text in segments:
            tok = self.estimate_tokens(text)
            if merged:
                p_start, p_end, p_text = merged[-1]
                p_tok = self.estimate_tokens(p_text)
                if p_tok < self._min_tokens and p_tok + tok <= merge_ceiling:
                    merged[-1] = (p_start, end, p_text + "\n\n" + text)
                    continue
            merged.append((start, end, text))

        # Per segment, delegate to RecursiveChunker. Tag every emitted chunk
        # with the owning segment's start/end seconds. Header (frontmatter
        # before the first timestamp) is prepended to the first segment.
        out: list[Chunk] = []
        for i, (start, end, text) in enumerate(merged):
            body = (header + "\n\n" + text) if (header and i == 0) else text
            sub_chunks = self._recursive.chunk(body, metadata)
            for sc in sub_chunks:
                sc.metadata = {
                    **sc.metadata,
                    "start_time": _format_time(start),
                    "end_time": _format_time(end),
                    "start_seconds": start,
                    "end_seconds": end,
                }
                out.append(sc)

        # Renumber chunk_index / total_chunks across the flat output list.
        for i, c in enumerate(out):
            c.metadata["chunk_index"] = i
            c.metadata["total_chunks"] = len(out)
            c.metadata["tokens"] = self.estimate_tokens(c.text)

        return out
