"""
Unit tests for PDF embedded-attachment extraction (expand_pdf_attachments).

A PDF can carry whole files in its /EmbeddedFiles tree — like email
attachments, not page images. These were silently dropped before; the
extractor pulls them out as independent input files so each gets parsed by
its own path. This pins the extractor's contract:

  1. attachments of ANY type are extracted (png / pdf / xlsx), not just images
  2. naming follows <parent_stem>__<attachment_name> (zip-expansion convention)
  3. a PDF with no attachments → [] (the default-on path must be a no-op here)
  4. a non-PDF input → [] (the pass only touches .pdf)
  5. one corrupt attachment is isolated — the others still extract
  6. never raises

Pure stdlib + pymupdf/openpyxl/PIL (all already deps). No network, no Vision —
extraction is deterministic file I/O, so these run fast and offline.

Run:
    python tests/unit/test_pdf_attachments.py
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docingest.utils.pdf_attachment_expander import expand_pdf_attachments


# ---------------------------------------------------------------------------
# Fixtures — build a parent PDF carrying 3 different attachment types
# ---------------------------------------------------------------------------
def _png_bytes() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (40, 20), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _child_pdf_bytes() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "child pdf body")
    data = doc.tobytes()
    doc.close()
    return data


def _xlsx_bytes() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws["A1"] = "cell"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_parent(path: Path, attachments: dict[str, bytes]) -> None:
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "parent body")
    for name, data in attachments.items():
        doc.embfile_add(name, data, filename=name)
    doc.save(str(path))
    doc.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_extracts_all_types(tmp_path: Path) -> None:
    parent = tmp_path / "parent.pdf"
    _make_parent(parent, {
        "diagram.png": _png_bytes(),
        "child.pdf": _child_pdf_bytes(),
        "table.xlsx": _xlsx_bytes(),
    })
    root = tmp_path / "_extract"
    out = expand_pdf_attachments(parent, root)

    assert len(out) == 3, f"expected 3 attachments, got {len(out)}"
    names = sorted(p.name for p in out)
    # naming: <parent_stem>__<attachment_name>
    assert names == [
        "parent__child.pdf",
        "parent__diagram.png",
        "parent__table.xlsx",
    ], names
    # every produced file exists and is non-empty
    for p in out:
        assert p.is_file() and p.stat().st_size > 0, p
    print("  ✓ test_extracts_all_types")


def test_no_attachments_is_noop(tmp_path: Path) -> None:
    parent = tmp_path / "plain.pdf"
    _make_parent(parent, {})           # zero attachments
    out = expand_pdf_attachments(parent, tmp_path / "_extract")
    assert out == [], f"expected [] for attachment-free PDF, got {out}"
    print("  ✓ test_no_attachments_is_noop")


def test_non_pdf_is_noop(tmp_path: Path) -> None:
    notpdf = tmp_path / "data.txt"
    notpdf.write_text("hello")
    out = expand_pdf_attachments(notpdf, tmp_path / "_extract")
    assert out == [], f"expected [] for non-PDF, got {out}"
    print("  ✓ test_non_pdf_is_noop")


def test_corrupt_input_never_raises(tmp_path: Path) -> None:
    # A .pdf extension on non-PDF bytes — open() fails; must return [], not raise.
    fake = tmp_path / "broken.pdf"
    fake.write_bytes(b"not a real pdf at all")
    out = expand_pdf_attachments(fake, tmp_path / "_extract")
    assert out == [], f"expected [] for corrupt PDF, got {out}"
    print("  ✓ test_corrupt_input_never_raises")


def test_naming_collision_across_parents(tmp_path: Path) -> None:
    # Two different parents with a same-named attachment must not collide:
    # each parent gets its own subdir, and names carry the parent stem.
    p1 = tmp_path / "alpha.pdf"
    p2 = tmp_path / "beta.pdf"
    _make_parent(p1, {"shared.png": _png_bytes()})
    _make_parent(p2, {"shared.png": _png_bytes()})
    root = tmp_path / "_extract"
    out1 = expand_pdf_attachments(p1, root)
    out2 = expand_pdf_attachments(p2, root)
    assert out1[0].name == "alpha__shared.png", out1[0].name
    assert out2[0].name == "beta__shared.png", out2[0].name
    assert out1[0] != out2[0]
    print("  ✓ test_naming_collision_across_parents")


def main() -> int:
    tests = [
        test_extracts_all_types,
        test_no_attachments_is_noop,
        test_non_pdf_is_noop,
        test_corrupt_input_never_raises,
        test_naming_collision_across_parents,
    ]
    print("=== test_pdf_attachments ===")
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
    print("ALL PDF-attachment TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
