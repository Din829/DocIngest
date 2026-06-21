"""
Azure DI AnalyzeResult → DocIngest ParseResult mapping (pure functions).

Kept SDK-free on purpose: this module never imports the azure SDK, so it can
be unit-tested with a hand-built / recorded AnalyzeResult-shaped object and no
network. The parser (``di_parser.py``) owns the network call; this module owns
the translation.

What Azure DI gives us (model="prebuilt-layout",
output_content_format=DocumentContentFormat.MARKDOWN):
  - result.content : the WHOLE document as one Markdown string. Headings,
    lists and section structure are already Markdown. Two DI-specific quirks
    we normalise here:
      * Tables are emitted as HTML ``<table>`` blocks (v4.0 GA, to represent
        merged cells / multi-row headers), NOT pipe tables.
      * Page header/footer lines appear inline as ``PageHeader="..."`` /
        ``PageFooter="..."`` markers.
  - result.pages : per-page objects carrying ``spans`` (offset+length into
    ``content``) plus width/height/page_number. We use the spans to split the
    single content string into per-page text for DocIngest's PageData.

Design choices (documented so a maintainer can change them with eyes open):
  - We do NOT re-flow or re-transcribe anything. DI's Markdown is taken as the
    body verbatim except for the two normalisations above.
  - Page splitting uses spans when present; when a page has no spans (rare,
    e.g. a blank page) it contributes an empty PageData so page_no stays
    aligned with DI's own numbering.
"""

from __future__ import annotations

import re
from typing import Any

from ..parsers.base import PageData, PAGEBREAK_MARKER

# DI inline page furniture markers, e.g.  PageHeader="..."  /  PageFooter="..."
# Anchored to line start so we don't strip a legitimate occurrence mid-sentence.
_PAGE_FURNITURE_RE = re.compile(
    r'^(?:PageHeader|PageFooter|PageNumber)="(?P<text>.*)"\s*$',
    re.MULTILINE,
)

# Cheap structural signals for metadata flags. has_tables keys off DI's HTML
# table emission; has_images off the figure block DI wraps charts/images in.
_HTML_TABLE_RE = re.compile(r"<table[\s>]", re.IGNORECASE)
_FIGURE_RE = re.compile(r"<figure[\s>]", re.IGNORECASE)


def strip_page_furniture(markdown: str) -> str:
    """Remove DI's inline ``PageHeader=/PageFooter=/PageNumber=`` marker lines.

    DocIngest treats repeating page furniture separately (see the optional
    strip_repeating hook); DI's quoted-marker form is noise in the body. We
    drop only whole marker lines, never inline text, so document content is
    untouched.
    """
    cleaned = _PAGE_FURNITURE_RE.sub("", markdown)
    # Collapse the blank lines the removal may leave behind (3+ → 2).
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def _page_text(content: str, page: Any) -> str:
    """Extract this page's slice of ``content`` using DI's spans.

    DI spans are {offset, length} into the top-level content string. A page
    can carry multiple spans (rare); we concatenate them in order. Returns ""
    when the page has no spans (blank page) — the caller still emits a
    PageData so numbering stays 1:1 with DI.
    """
    spans = getattr(page, "spans", None) or []
    parts: list[str] = []
    for span in spans:
        # span may be an SDK model (attrs) or a plain dict (recorded fixture).
        offset = _get(span, "offset")
        length = _get(span, "length")
        if offset is None or length is None:
            continue
        parts.append(content[offset : offset + length])
    return "".join(parts)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an SDK model (attribute) or a dict (subscript)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_pages(content: str, result: Any) -> list[PageData]:
    """Build DocIngest PageData list from DI pages + content spans.

    text is DI's own per-page slice (already OCR'd by the cloud service).
    image_path is left empty: Azure DI is a cloud OCR+layout engine, so the
    DocIngest per-page Vision step is redundant on this path (and there is no
    local render to point at). The pipeline tolerates empty image_path — Vision
    simply has nothing to enrich, which is the intended behaviour here.
    """
    pages = getattr(result, "pages", None) or []
    out: list[PageData] = []
    for page in pages:
        page_no = _get(page, "page_number") or (len(out) + 1)
        out.append(PageData(page_no=int(page_no), text=_page_text(content, page)))
    return out


def join_page_markdown(pages: list[PageData]) -> str:
    """Stitch per-page text back into one markdown string with pagebreaks.

    Used only when we choose to rebuild the body from page slices. The default
    path keeps DI's full ``content`` as the body (richer than concatenated
    slices, which can drop cross-page furniture DI placed outside any span);
    this helper exists for callers that want strict page-delimited output.
    """
    return f"\n{PAGEBREAK_MARKER}\n".join(p.text for p in pages)


def convert(result: Any, *, file_path: Any, source_format: str) -> dict[str, Any]:
    """Translate a DI AnalyzeResult into the pieces a ParseResult needs.

    Returns a dict with keys: markdown, pages, metadata. The parser wraps
    these into a ParseResult — keeping this function return-a-dict makes it
    trivially unit-testable without constructing ParseResult.

    Args:
        result: an AnalyzeResult (SDK) or a recorded fixture with the same
            shape (.content, .pages).
        file_path: pathlib.Path of the input — used for title/format only.
        source_format: the resolved format string (e.g. "pdf") for metadata.
    """
    raw = _get(result, "content") or ""
    markdown = strip_page_furniture(raw)
    pages = build_pages(raw, result)

    metadata: dict[str, Any] = {
        "format": source_format,
        "title": getattr(file_path, "stem", str(file_path)),
        "parser_engine": "azure_di",
        "has_tables": bool(_HTML_TABLE_RE.search(markdown)),
        "has_images": bool(_FIGURE_RE.search(markdown)),
    }
    if pages:
        metadata["pages"] = len(pages)

    return {"markdown": markdown, "pages": pages, "metadata": metadata}
