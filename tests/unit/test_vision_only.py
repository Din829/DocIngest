"""
VisionOnlyParser — offline tests (no Vision/network; PyMuPDF only).

Verifies the vision_only engine: PDF/image render to page images with empty
text (Vision fills later), non-page-image formats delegate to the Docling path,
the ParseResult contract is intact, and the engine routes correctly. PyMuPDF /
test-PDF absence self-skips the render test.

Run:
    python tests/unit/test_vision_only.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.parsers.vision_only_parser import VisionOnlyParser, _PAGE_IMAGE_FORMATS

_ROOT = Path(__file__).resolve().parent.parent.parent
_PDF_CANDIDATES = [
    _ROOT / "test_docs" / "1612752_82335184.pdf",
    _ROOT / "test_docs" / "2" / "0253324_契約金明細書304.pdf",
]


def _pick_pdf():
    for p in _PDF_CANDIDATES:
        if p.exists():
            return p
    return None


def _have_pymupdf() -> bool:
    try:
        import pymupdf  # noqa: F401
        return True
    except ImportError:
        try:
            import fitz  # noqa: F401
            return True
        except ImportError:
            return False


def _cfg(out_dir: str) -> dict:
    return {
        "parsing": {"engine": "vision_only", "vision": {"image_dpi": 180}},
        "output": {"dir": out_dir, "assets_dir": "assets"},
    }


# --- routing ---------------------------------------------------------------

def test_engine_routes_vision_only() -> None:
    from docingest.parsers import create_parser
    p = create_parser({"parsing": {"engine": "vision_only"}})
    assert type(p).__name__ == "VisionOnlyParser"
    print("ok: engine=vision_only routes to VisionOnlyParser")


def test_default_engine_unchanged() -> None:
    from docingest.parsers import create_parser
    p = create_parser({"parsing": {"engine": "docling"}})
    assert type(p).__name__ == "_DoclingWithFallback"
    print("ok: default engine still _DoclingWithFallback (no regression)")


# --- PDF: render, empty text, no Docling -----------------------------------

def test_pdf_renders_pages_with_empty_text() -> None:
    if not _have_pymupdf():
        print("ok: skipped (PyMuPDF absent)")
        return
    pdf = _pick_pdf()
    if pdf is None:
        print("ok: skipped (no test PDF)")
        return

    import pymupdf
    with pymupdf.open(str(pdf)) as d:
        expected = d.page_count

    with tempfile.TemporaryDirectory() as td:
        parser = VisionOnlyParser(_cfg(td))
        result = parser.parse(pdf)

        assert result.success is True
        assert result.metadata["parser_engine"] == "vision_only"
        assert result.metadata["format"] == "pdf"
        assert result.metadata["pages"] == expected
        assert len(result.pages) == expected
        # Every page: empty text (Vision fills later), real image path, visual flag.
        for pg in result.pages:
            assert pg.text == "", "vision_only must leave page text empty for Vision"
            assert pg.image_path and Path(pg.image_path).exists()
            assert pg.num_pictures == 1, "page must be marked visual so triage never skips it"
        # markdown is page-delimited placeholders (N-1 pagebreaks for N pages)
        from docingest.parsers.base import PAGEBREAK_MARKER
        assert result.markdown.count(PAGEBREAK_MARKER) == expected - 1
        # transformation trail records vision_only
        assert any(t.get("name") == "vision_only" for t in result.transformations)
    print(f"ok: PDF rendered {expected} pages, empty text, no Docling ({pdf.name})")


# --- non-page-image format delegates ---------------------------------------

class _StubDelegate:
    """Stand-in for _DoclingWithFallback to prove delegation without Docling."""
    def __init__(self) -> None:
        self.parsed = None

    def parse(self, file_path, *, override_stream=None):
        from docingest.parsers.base import ParseResult
        self.parsed = file_path
        return ParseResult(markdown="delegated", success=True, metadata={"format": "txt"})

    def supported_extensions(self):
        return {".txt", ".md"}


def test_non_page_image_format_delegates() -> None:
    with tempfile.TemporaryDirectory() as td:
        parser = VisionOnlyParser(_cfg(td))
        stub = _StubDelegate()
        parser._delegate = stub  # inject so no real Docling is built

        result = parser.parse(Path("notes.txt"))
        assert result.markdown == "delegated"
        assert stub.parsed == Path("notes.txt"), "txt must be delegated to Docling path"
    print("ok: non-page-image format (txt) delegates to Docling path")


def test_xlsx_delegates_not_visioned() -> None:
    # The moat case: xlsx must NOT go vision-only (would throw its accurate
    # openpyxl tables at Vision). It must delegate.
    with tempfile.TemporaryDirectory() as td:
        parser = VisionOnlyParser(_cfg(td))
        stub = _StubDelegate()
        parser._delegate = stub
        parser.parse(Path("book.xlsx"))
        assert stub.parsed == Path("book.xlsx"), "xlsx must delegate (protect openpyxl moat)"
    print("ok: xlsx delegates (openpyxl moat protected, not vision-only)")


def test_page_image_format_set() -> None:
    # Guard the scope: PDF + common image types are page-image; office/text are not.
    assert ".pdf" in _PAGE_IMAGE_FORMATS
    assert ".png" in _PAGE_IMAGE_FORMATS and ".jpg" in _PAGE_IMAGE_FORMATS
    assert ".xlsx" not in _PAGE_IMAGE_FORMATS
    assert ".docx" not in _PAGE_IMAGE_FORMATS
    assert ".txt" not in _PAGE_IMAGE_FORMATS
    print("ok: page-image format set scoped correctly (pdf/img in, office/text out)")


# --- image input -----------------------------------------------------------

def test_image_input_staged_as_page() -> None:
    # Build a tiny PNG to act as an image input.
    try:
        from PIL import Image
    except ImportError:
        print("ok: skipped (PIL absent)")
        return
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "scan.png"
        Image.new("RGB", (64, 48), "white").save(src)

        parser = VisionOnlyParser(_cfg(td))
        result = parser.parse(src)
        assert result.success is True
        assert len(result.pages) == 1
        assert result.pages[0].text == ""
        assert Path(result.pages[0].image_path).exists()
        assert result.metadata["parser_engine"] == "vision_only"
    print("ok: image input staged as one Vision page")


if __name__ == "__main__":
    test_engine_routes_vision_only()
    test_default_engine_unchanged()
    test_pdf_renders_pages_with_empty_text()
    test_non_page_image_format_delegates()
    test_xlsx_delegates_not_visioned()
    test_page_image_format_set()
    test_image_input_staged_as_page()
    print("\nALL VISION_ONLY TESTS PASSED")
