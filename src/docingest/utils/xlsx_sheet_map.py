"""
xlsx sheet → PDF page mapping via the PDF outline that LibreOffice
emits when it renders a Calc document.

Why this module exists
----------------------
When a single xlsx sheet renders to multiple PDF pages, downstream code
needs to know which PDF pages belong to which sheet — Vision batched
mode wants per-sheet grouping, the connector hook needs to anchor
diagram edges to the right starting page, and any future sheet-aware
behaviour needs the same map. LibreOffice's calc_pdf_Export emits a
level-1 PDF bookmark per rendered sheet; reading those bookmarks is
the cheapest, least-invasive way to recover the mapping.

Recall is sacred — DO NOT use this map to skip work
---------------------------------------------------
This map is an optional QUALITY improvement, not a gate. If the
outline is missing, malformed, or a sheet doesn't match (LibreOffice
sometimes normalises whitespace in titles), the function returns an
empty or partial dict. Downstream callers MUST treat "sheet absent
from map" as "fall back to the legacy 1-sheet-1-page assumption",
never as "skip this page". Pages without a sheet mapping still go
through Vision exactly as they did before this feature existed.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _normalize(s: str) -> str:
    """
    Collapse internal whitespace runs and strip the ends.

    Byte-level verified against six real-world xlsx files: LibreOffice
    drops trailing spaces in PDF outline titles ('製品カット) ' →
    '製品カット)') and collapses runs of internal whitespace to a single
    space ('38  横並び' → '38 横並び'). Normalising both sides before
    comparison recovers those matches; without this every xlsx with a
    space-padded sheet name would be a silent mis-mapping.
    """
    return re.sub(r"\s+", " ", s).strip()


def build_sheet_page_map(
    pdf_path: Path,
    visible_sheet_names: list[str],
) -> dict[str, int]:
    """
    Returns ``{original_sheet_name: first_page_no_in_pdf}``.

    The dict key preserves the un-normalised sheet name so callers can
    look up directly by openpyxl's sheet name; only the comparison key
    is normalised internally.

    Args:
        pdf_path: PDF produced by LibreOffice from the source xlsx.
        visible_sheet_names: Sheet names in workbook order, hidden
            sheets already filtered out by the caller.

    Returns:
        Empty dict on any failure (pymupdf missing, no outline, file
        unreadable). Partial dict when some sheets match and some
        don't — common when a sheet has zero renderable content
        (LibreOffice omits it from the PDF, so it has no bookmark).
        Callers MUST handle absent keys via fallback, not by
        skipping the page.
    """
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError:
        logger.debug(
            "sheet→page mapping: pymupdf not installed — skipping. "
            "Downstream falls back to the legacy 1-sheet-1-page assumption."
        )
        return {}

    if not pdf_path.exists():
        logger.debug(f"sheet→page mapping: pdf not found at {pdf_path}")
        return {}

    try:
        doc = fitz.open(str(pdf_path))
        try:
            toc = doc.get_toc()
        finally:
            doc.close()
    except Exception as e:
        logger.warning(
            f"sheet→page mapping: cannot read outline from {pdf_path.name} "
            f"({type(e).__name__}: {e}) — downstream falls back to the "
            f"legacy 1-sheet-1-page assumption"
        )
        return {}

    if not toc:
        logger.debug(
            f"sheet→page mapping: no outline in {pdf_path.name} — downstream "
            f"falls back to the legacy 1-sheet-1-page assumption"
        )
        return {}

    # LibreOffice emits one level-1 bookmark per rendered sheet. Defending
    # against future LO behaviour change (sub-headings, deeper nesting):
    # only consume level==1 entries.
    norm_to_original: dict[str, str] = {_normalize(s): s for s in visible_sheet_names}

    mapping: dict[str, int] = {}
    for entry in toc:
        # toc entries are [level, title, page_no_1_based] (extra fields may
        # exist on newer pymupdf — index defensively).
        if len(entry) < 3:
            continue
        level, title, page_no = entry[0], entry[1], entry[2]
        if level != 1 or not isinstance(title, str):
            continue
        original = norm_to_original.get(_normalize(title))
        if original is None:
            # Outline entry for a sheet we don't know about — could be a
            # hidden sheet LO surfaced anyway, or a sheet renamed since
            # the xlsx was modified in place. Skip rather than guess.
            continue
        # First occurrence wins: protects against duplicate bookmarks
        # (some LO versions emit a second bookmark for very wide sheets).
        try:
            page_no_int = int(page_no)
        except (TypeError, ValueError):
            continue
        mapping.setdefault(original, page_no_int)

    matched = len(mapping)
    total = len(visible_sheet_names)
    logger.info(
        f"sheet→page mapping: matched {matched}/{total} sheet(s) from "
        f"{pdf_path.name} outline ({len(toc)} outline entry/entries)"
    )
    return mapping
