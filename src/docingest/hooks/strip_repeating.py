# -*- coding: utf-8 -*-
"""
Repeating header/footer stripper — pre_write hook.

Documents carry per-page furniture (watermarks, logos, page numbers, running
dates) that docling's content_layer filtering misses when the text came from
Vision — Vision transcribes the WHOLE page image, furniture included. This
hook removes that noise from the assembled markdown BEFORE chunking, so RAG
chunks aren't polluted by lines like "DocuSign Envelope ID: ..." or a bare
"PwC" repeated on every page.

Design principles (mirrors sanitize.py):
  - Precision over recall: only remove lines that are near-certainly furniture.
    Better to leave a stray header than to delete one line of body text.
  - Default OFF: opt-in via hooks.strip_repeating.enabled. Off → zero change.
  - Conservative rules:
      1. Lines repeating VERBATIM (whitespace-normalized) across >= N pages
         are DEDUPLICATED: the FIRST occurrence is kept, only later copies are
         dropped. We must NOT delete every copy — some body text also repeats
         verbatim (a note printed under each section; a recipient / company
         header on every page of a short contract). Keeping the first copy
         guarantees no unique content is ever lost, while still collapsing N
         copies of true furniture (logos, envelope IDs) down to one.
      2. (opt-in, OFF by default) Standalone 1-3 digit lines — page numbers.
         A bare number line is almost always a page number, but "almost" is
         not "never" (a body figure can land alone on a line), so deleting it
         is not 100% safe — hence opt-in.
    Both skip markdown structure (tables / headings / code / lists / html
    markers) and rule 1 only considers short lines, so body text is untouched.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult, PAGEBREAK_MARKER

logger = logging.getLogger(__name__)

_PAGE_NUM_RE = re.compile(r"^\s*\d{1,3}\s*$")


def _normalize(line: str) -> str:
    """Whitespace-stripped key for verbatim cross-page comparison."""
    return re.sub(r"\s+", "", line)


def _is_structural(stripped: str) -> bool:
    """Markdown structure / non-body lines we must never touch."""
    if not stripped:
        return True
    return (
        stripped[0] in "#|>*-"            # heading / table / quote / list
        or stripped.startswith("```")     # code fence
        or stripped.startswith("<!--")    # html marker (pagebreak / image / vision)
    )


def strip_repeating_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """
    Pre-write hook: drop repeating per-page furniture from the markdown.

    Raises HookNoOp (leaves markdown untouched, no lineage entry) when:
      - disabled (default),
      - the doc has no pagebreaks / too few pages to judge repetition,
      - nothing was removed.
    """
    from . import HookNoOp

    cfg = get_nested(config, "hooks.strip_repeating", {}) or {}
    if not cfg.get("enabled", False):
        raise HookNoOp

    md = parse_result.markdown
    if PAGEBREAK_MARKER not in md:
        raise HookNoOp  # single page → no cross-page repetition to detect

    min_pages = int(cfg.get("min_repeat_pages", 3))
    max_chars = int(cfg.get("max_line_chars", 120))
    strip_page_nums = bool(cfg.get("strip_page_numbers", False))

    # Process line-by-line and keep pagebreak markers VERBATIM. Splitting on
    # PAGEBREAK_MARKER and re-joining drops the newlines around it and glues
    # the marker onto adjacent text — corrupting the page structure that
    # downstream slide/sheet chunkers rely on (they split on standalone
    # `<!-- pagebreak -->` lines).
    lines = md.splitlines()

    def _is_pagebreak(line: str) -> bool:
        return PAGEBREAK_MARKER in line

    num_pages = 1 + sum(1 for l in lines if _is_pagebreak(l))
    if num_pages < min_pages:
        raise HookNoOp  # too few pages for a reliable repetition signal

    # Page index for each line (incremented after each pagebreak line).
    line_page: list[int] = []
    _pidx = 0
    for l in lines:
        line_page.append(_pidx)
        if _is_pagebreak(l):
            _pidx += 1

    # Count on how many distinct pages each short, non-structural line appears.
    line_pages: dict[str, set[int]] = defaultdict(set)
    for i, l in enumerate(lines):
        if _is_pagebreak(l):
            continue
        s = l.strip()
        if _is_structural(s) or len(s) > max_chars:
            continue
        norm = _normalize(l)
        if norm:
            line_pages[norm].add(line_page[i])
    repeating = {n for n, ps in line_pages.items() if len(ps) >= min_pages}

    seen_repeat: set[str] = set()     # repeating keys already kept once
    removed = 0
    kept: list[str] = []
    for l in lines:
        if _is_pagebreak(l):
            kept.append(l)            # never touch pagebreak markers
            continue
        s = l.strip()
        # Rule 1: verbatim cross-page repeat (watermark / logo / running date).
        # DEDUP — keep the FIRST occurrence, drop only the later copies. This
        # guarantees no unique content is ever lost: body text that happens to
        # repeat verbatim (per-section notes, a short contract's header) keeps
        # its first copy; true furniture collapses from N copies to one.
        norm = _normalize(l)
        if (
            s
            and not _is_structural(s)
            and len(s) <= max_chars
            and norm in repeating
        ):
            if norm in seen_repeat:
                removed += 1
                continue
            seen_repeat.add(norm)
            kept.append(l)            # keep the first occurrence
            continue
        # Rule 2 (opt-in): standalone short page number
        if strip_page_nums and _PAGE_NUM_RE.match(l):
            removed += 1
            continue
        kept.append(l)

    if removed == 0:
        raise HookNoOp

    parse_result.markdown = "\n".join(kept)
    logger.info(
        f"strip_repeating: removed {removed} repeating header/footer line(s) "
        f"in {file_path.name}"
    )
