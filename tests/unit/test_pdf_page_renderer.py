"""
PDF page-image render via PyMuPDF (_render_pdf_pages_pymupdf) — offline.

Renders a real (small) PDF and checks: one PNG per page, the Docling-compatible
filename pattern, files actually exist + are non-trivial, and the 4 MP cap holds.
No Vision / network. If PyMuPDF or a test PDF is absent the test self-skips.

Run:
    python tests/unit/test_pdf_page_renderer.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.parsers.docling_parser import DoclingParser

_ROOT = Path(__file__).resolve().parent.parent.parent
# A small real PDF from the repo's test corpus.
_CANDIDATES = [
    _ROOT / "test_docs" / "1612752_82335184.pdf",
    _ROOT / "test_docs" / "2" / "0253324_契約金明細書304.pdf",
]


def _pick_pdf() -> Path | None:
    for p in _CANDIDATES:
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


def test_pymupdf_render_produces_one_png_per_page() -> None:
    if not _have_pymupdf():
        print("ok: skipped (PyMuPDF not installed)")
        return
    pdf = _pick_pdf()
    if pdf is None:
        print("ok: skipped (no test PDF found)")
        return

    import pymupdf
    with pymupdf.open(str(pdf)) as d:
        expected_pages = d.page_count

    with tempfile.TemporaryDirectory() as td:
        assets = Path(td)
        out = DoclingParser._render_pdf_pages_pymupdf(pdf, assets, image_dpi=180)

        assert len(out) == expected_pages, (
            f"expected {expected_pages} page images, got {len(out)}"
        )
        # Docling-compatible filename pattern + files exist + non-trivial size
        for page_no, path in out.items():
            p = Path(path)
            assert p.exists(), f"page image not written: {path}"
            assert p.name == f"{pdf.stem}-page-{page_no:03d}.png", (
                f"unexpected filename: {p.name}"
            )
            assert p.stat().st_size > 1000, f"page image suspiciously small: {path}"

        # 4 MP cap: open one and check it's within bounds. Use a `with` so the
        # file handle is released before TemporaryDirectory cleanup (Windows
        # can't unlink a file PIL still holds open).
        from PIL import Image
        with Image.open(next(iter(out.values()))) as first:
            w, h = first.size
        assert w * h <= 4_000_000, f"page image exceeds 4 MP cap: {w}x{h} = {w*h}"
        print(f"ok: rendered {len(out)} pages, naming + 4MP cap correct "
              f"(sample {w}x{h}, {pdf.name})")


def test_empty_dict_on_missing_pymupdf(monkeypatch=None) -> None:
    # Simulate PyMuPDF absent → function returns {} (caller falls back to Docling).
    import builtins
    real_import = builtins.__import__

    def _block(name, *a, **k):
        if name in ("pymupdf", "fitz"):
            raise ImportError("simulated absent")
        return real_import(name, *a, **k)

    builtins.__import__ = _block
    try:
        with tempfile.TemporaryDirectory() as td:
            out = DoclingParser._render_pdf_pages_pymupdf(
                Path("nonexistent.pdf"), Path(td), image_dpi=180
            )
        assert out == {}, "should return empty dict when PyMuPDF is unavailable"
    finally:
        builtins.__import__ = real_import
    print("ok: returns {} (fail soft) when PyMuPDF unavailable")


if __name__ == "__main__":
    test_pymupdf_render_produces_one_png_per_page()
    test_empty_dict_on_missing_pymupdf()
    print("\nALL PDF PAGE RENDERER TESTS PASSED")
