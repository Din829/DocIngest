"""
Heading-based chunker — splits by Markdown headings, then recursive within sections.

Strategy:
  1. Split document at heading boundaries (configurable levels: H1, H2, H3)
  2. Each section is classified as prelude / heading_only / normal
  3. Single-pass merge loop combines small / orphan sections into the next
     normal section (forward-merge), giving a stable title_path and never
     leaving 3-token "西暦" fragments at the head of the file.
  4. Sections larger than max_tokens fall through to RecursiveChunker.

Earlier revisions had TWO independent merge passes (pending_prefix +
small-section accumulator). They interacted poorly — producing duplicate
chunks and swallowing the deepest title_path. This revision folds both
into a single accumulator with explicit policies (config-driven):

  prelude_policy        attach_to_first | standalone | drop
  orphan_heading_policy merge_forward   | keep
  title_path_strategy   deepest         | first | join_all

So the same code handles Japanese legal docs, English tech reports,
and whatever the next project throws at it — you only change config.
"""

from __future__ import annotations

import re
from typing import Any

from .base import BaseChunker, Chunk
from .recursive import RecursiveChunker


# Heading pattern: # Title, ## Title, ### Title, etc.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


class HeadingChunker(BaseChunker):
    """Split by Markdown headings, recursive within sections."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)

        heading_cfg = self._chunking.get("heading", {}) or {}
        self._levels = set(heading_cfg.get("levels", [1, 2, 3]))
        self._fallback_strategy = heading_cfg.get("fallback", "recursive")
        self._merge_small = bool(heading_cfg.get("merge_small_sections", True))
        self._prelude_policy = heading_cfg.get("prelude_policy", "attach_to_first")
        self._orphan_policy = heading_cfg.get("orphan_heading_policy", "merge_forward")
        self._title_strategy = heading_cfg.get("title_path_strategy", "deepest")

        # Recursive chunker for subdividing large sections
        self._recursive = RecursiveChunker(config)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def chunk(self, markdown: str, metadata: dict[str, Any]) -> list[Chunk]:
        if not markdown.strip():
            return []

        sections = self._split_by_headings(markdown)
        if not sections:
            return self._recursive.chunk(markdown, metadata)

        # Classify, then run a single-pass merge.
        classified = [self._classify(s) for s in sections]
        all_chunks = self._merge_and_emit(classified, metadata)

        # Renumber after all merging is done.
        for i, c in enumerate(all_chunks):
            c.metadata["chunk_index"] = i
            c.metadata["total_chunks"] = len(all_chunks)
            c.metadata["tokens"] = self.estimate_tokens(c.text)

        return all_chunks

    # ------------------------------------------------------------------
    # Section classification
    # ------------------------------------------------------------------

    def _classify(self, section: dict[str, Any]) -> dict[str, Any]:
        """
        Tag a section with:
          kind: "prelude" | "heading_only" | "normal"
          tokens: int
        plus carry through text and title_path.

        prelude      — no heading line anywhere in the section (text
                       that appeared before the first #/##/### in the doc)
        heading_only — starts with a heading and has no meaningful body
                       (ignore blank lines, HTML comments)
        normal       — everything else
        """
        text = section["text"]
        title_path = section.get("title_path", "")
        lines = text.strip().split("\n") if text.strip() else []

        has_heading = any(_HEADING_RE.match(ln) for ln in lines)
        # Lines that count as "real body content"
        body_lines = [
            ln for ln in lines
            if ln.strip()
            and not _HEADING_RE.match(ln)
            and not ln.strip().startswith("<!--")
        ]

        if not has_heading and not title_path:
            kind = "prelude"
        elif has_heading and not body_lines:
            kind = "heading_only"
        else:
            kind = "normal"

        return {
            "text": text,
            "title_path": title_path,
            "kind": kind,
            "tokens": self.estimate_tokens(text),
        }

    # ------------------------------------------------------------------
    # Merge + emit
    # ------------------------------------------------------------------

    def _merge_and_emit(
        self,
        sections: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> list[Chunk]:
        """
        Walk sections and emit Chunks. Uses one accumulator that holds
        pending prelude / heading_only / small sections until the next
        normal-sized section arrives — then flushes them together.
        """
        out: list[Chunk] = []
        pending: list[dict[str, Any]] = []

        for section in sections:
            kind = section["kind"]

            # Prelude handling (config-driven)
            if kind == "prelude":
                if self._prelude_policy == "drop":
                    continue
                if self._prelude_policy == "standalone":
                    out.append(self._to_chunk(section, metadata))
                    continue
                # attach_to_first (default) → queue for next normal section
                pending.append(section)
                continue

            # Orphan heading (heading with no body)
            if kind == "heading_only":
                if self._orphan_policy == "keep":
                    out.append(self._to_chunk(section, metadata))
                    continue
                # merge_forward (default)
                pending.append(section)
                continue

            # Normal section — combine with anything pending
            combined_text, combined_title = self._combine(pending + [section])
            combined_tokens = self.estimate_tokens(combined_text)
            pending = []

            # Too big → hand to recursive chunker, title_path propagates
            if combined_tokens > self._max_tokens:
                sub_chunks = self._recursive.chunk(
                    combined_text,
                    {**metadata, "title_path": combined_title},
                )
                out.extend(sub_chunks)
                continue

            # Small — hold for merging with next section. Works for the
            # first section too (out may be empty): the accumulator will
            # simply keep growing until a normal-sized section arrives or
            # the loop ends (tail-flush merges into the last output chunk).
            if self._merge_small and combined_tokens < self._min_tokens:
                pending.append({
                    "text": combined_text,
                    "title_path": combined_title,
                    "kind": "normal",
                    "tokens": combined_tokens,
                })
                continue

            out.append(Chunk(
                text=combined_text,
                metadata={**metadata, "title_path": combined_title},
            ))

        # Flush any leftover pending. Three cases:
        #   1. output exists + pending is small enough to glue on → forward-
        #      merge into the last chunk (avoids trailing fragments).
        #   2. output exists + pending is big enough to warrant its own chunk
        #      (or its own sub-chunks via recursive) → emit separately.
        #   3. output is empty (document had no normal section ever — e.g.
        #      wall-of-text with no headings, or all sections were tiny) →
        #      run the combined text through the recursive chunker so it
        #      still gets size-limited.
        if pending:
            text, title = self._combine(pending)
            combined_tokens = self.estimate_tokens(text)
            base_meta = {**metadata, "title_path": title}
            if not out:
                if combined_tokens > self._max_tokens:
                    out.extend(self._recursive.chunk(text, base_meta))
                else:
                    out.append(Chunk(text=text, metadata=base_meta))
            elif combined_tokens <= self._max_tokens - self._min_tokens:
                # Small tail — safe to glue onto the last chunk without
                # pushing it far past max_tokens.
                last = out[-1]
                last.text = last.text + "\n\n" + text
                last.metadata["title_path"] = self._pick_title([
                    last.metadata.get("title_path", ""),
                    title,
                ])
            elif combined_tokens > self._max_tokens:
                out.extend(self._recursive.chunk(text, base_meta))
            else:
                out.append(Chunk(text=text, metadata=base_meta))

        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _combine(
        self,
        sections: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """Stitch sections together; choose the best title_path per config."""
        text = "\n\n".join(s["text"] for s in sections if s["text"].strip())
        title = self._pick_title([s["title_path"] for s in sections])
        return text, title

    def _pick_title(self, titles: list[str]) -> str:
        """
        Pick a title_path according to title_path_strategy:
          deepest   — the one with the most " > " segments (ties: last wins)
          first     — the first non-empty
          join_all  — all non-empty joined by " | " (legacy behaviour)
        """
        non_empty = [t for t in titles if t]
        if not non_empty:
            return ""
        strategy = self._title_strategy
        if strategy == "first":
            return non_empty[0]
        if strategy == "join_all":
            # Deduplicate while preserving order
            seen = set()
            ordered = []
            for t in non_empty:
                if t not in seen:
                    seen.add(t)
                    ordered.append(t)
            return " | ".join(ordered)
        # default: deepest (most granular title path wins)
        return max(non_empty, key=lambda t: (t.count(" > "), len(t)))

    def _to_chunk(
        self,
        section: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Chunk:
        return Chunk(
            text=section["text"],
            metadata={**metadata, "title_path": section["title_path"]},
        )

    # ------------------------------------------------------------------
    # Heading splitter (unchanged from previous revision)
    # ------------------------------------------------------------------

    def _split_by_headings(self, markdown: str) -> list[dict]:
        """
        Split markdown at heading boundaries.

        Returns list of {"text": "...", "title_path": "Section > Subsection"}.
        """
        lines = markdown.split("\n")
        sections: list[dict] = []
        current_lines: list[str] = []
        title_stack: list[tuple[int, str]] = []
        current_title_path = ""

        for line in lines:
            match = _HEADING_RE.match(line)
            if match:
                level = len(match.group(1))
                title = match.group(2).strip()

                if level in self._levels:
                    # Flush previous section
                    if current_lines:
                        text = "\n".join(current_lines).strip()
                        if text:
                            sections.append({
                                "text": text,
                                "title_path": current_title_path,
                            })
                        current_lines = []

                    # Update title stack
                    while title_stack and title_stack[-1][0] >= level:
                        title_stack.pop()
                    title_stack.append((level, title))

                    current_title_path = " > ".join(t for _, t in title_stack)

            current_lines.append(line)

        if current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                sections.append({
                    "text": text,
                    "title_path": current_title_path,
                })

        return sections
