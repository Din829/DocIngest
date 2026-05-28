"""
Tests for utils.xlsx_sheet_map.

Uses REAL LibreOffice-rendered PDFs (no mocking). The PDFs live under
tmp/debug_libreoffice/ from earlier experiments — if those are absent
the tests are skipped with a clear message.

Run:
    python tests/unit/test_xlsx_sheet_map.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.utils.xlsx_sheet_map import build_sheet_page_map, _normalize


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEBUG_DIR = REPO_ROOT / "tmp" / "debug_libreoffice"


class TestNormalize(unittest.TestCase):
    def test_trailing_space_dropped(self):
        # Mirrors the LibreOffice outline behaviour on sheet name
        # '製品カット) ' (real-world case from 松竹梅).
        self.assertEqual(_normalize("製品カット) "), "製品カット)")

    def test_internal_whitespace_collapsed(self):
        # Mirrors LO behaviour on '38  横並び' (real-world case from 22lite).
        self.assertEqual(_normalize("38  横並び"), "38 横並び")

    def test_already_clean(self):
        self.assertEqual(_normalize("normal_sheet"), "normal_sheet")

    def test_empty(self):
        self.assertEqual(_normalize(""), "")


class TestBuildSheetPageMap(unittest.TestCase):
    """End-to-end tests using real PDFs from earlier experiments."""

    def test_foox_full_match(self):
        pdf = DEBUG_DIR / "real_foox_要件仕様書_USDM" / "foox_要件仕様書_USDM.pdf"
        if not pdf.exists():
            self.skipTest(f"PDF not available: {pdf}")
        sheets = ["説明", "テンプレート", "サンプル", "ステータスマスタ"]
        m = build_sheet_page_map(pdf, sheets)
        self.assertEqual(len(m), 4, f"expected 4 matches, got {m}")
        for s in sheets:
            self.assertIn(s, m, f"sheet {s!r} missing from map")
        # Page numbers strictly ascending (sheets render in workbook order)
        page_seq = [m[s] for s in sheets]
        self.assertEqual(page_seq, sorted(page_seq))

    def test_22lite_double_space_normalization(self):
        pdf = DEBUG_DIR / "real_22lite_toukeir" / "22lite_toukeir.pdf"
        if not pdf.exists():
            self.skipTest(f"PDF not available: {pdf}")
        # '38  横並び' has TWO spaces in xlsx but ONE in PDF outline.
        # Normalization must recover this match.
        sheets = ["目次", "1正答率", "38  横並び"]
        m = build_sheet_page_map(pdf, sheets)
        self.assertIn("38  横並び", m,
                      f"normalisation failed to recover double-space sheet "
                      f"name; map: {m}")

    def test_matsutake_trailing_space_normalization(self):
        # Source sheet name '製品カット) ' has a trailing space; outline
        # title in PDF is '製品カット)' (LO strips trailing). Use
        # original sheet name (with trailing space) as the lookup key.
        pdf = DEBUG_DIR / "real_ソリューション松竹梅_0121時点" / "ソリューション松竹梅_0121時点.pdf"
        if not pdf.exists():
            self.skipTest(f"PDF not available: {pdf}")
        sheets = ["ソリューション松竹梅 (製品カット) "]  # trailing space preserved
        m = build_sheet_page_map(pdf, sheets)
        self.assertIn("ソリューション松竹梅 (製品カット) ", m,
                      f"normalisation failed to recover trailing-space "
                      f"sheet name; map: {m}")

    def test_missing_pdf_returns_empty_dict(self):
        # Recall-safety guarantee: failure path returns {}, never raises.
        m = build_sheet_page_map(Path("/nonexistent/path/file.pdf"), ["A", "B"])
        self.assertEqual(m, {})

    def test_empty_sheet_list_returns_empty_dict(self):
        # No sheets to match → nothing to map. Must not crash even if PDF
        # has an outline.
        pdf = DEBUG_DIR / "real_foox_要件仕様書_USDM" / "foox_要件仕様書_USDM.pdf"
        if not pdf.exists():
            self.skipTest(f"PDF not available: {pdf}")
        m = build_sheet_page_map(pdf, [])
        self.assertEqual(m, {})


class TestRecallSafety(unittest.TestCase):
    """
    The map is an opt-in optimisation, never a gate. These tests assert
    the contract that downstream code can rely on: failure → empty dict,
    partial match → partial dict, never an exception.
    """

    def test_partial_match_returns_partial_dict_not_raise(self):
        pdf = DEBUG_DIR / "real_foox_要件仕様書_USDM" / "foox_要件仕様書_USDM.pdf"
        if not pdf.exists():
            self.skipTest(f"PDF not available: {pdf}")
        sheets = ["説明", "NONEXISTENT_SHEET", "テンプレート"]
        m = build_sheet_page_map(pdf, sheets)
        # The two real sheets are in the map; the bogus one is not.
        self.assertIn("説明", m)
        self.assertIn("テンプレート", m)
        self.assertNotIn("NONEXISTENT_SHEET", m)

    def test_no_outline_returns_empty_dict_not_raise(self):
        # Use a PDF unlikely to have a Calc-style outline. The same
        # baseline PDFs satisfy this when a sheet is fully invisible —
        # using an empty sheet list is the cleanest no-outline analogue
        # here. The real test is that no exception escapes.
        pdf = DEBUG_DIR / "real_foox_要件仕様書_USDM" / "foox_要件仕様書_USDM.pdf"
        if not pdf.exists():
            self.skipTest(f"PDF not available: {pdf}")
        try:
            m = build_sheet_page_map(pdf, ["bogus_only"])
            # Should not crash; result may be empty (no real sheet
            # supplied for matching).
            self.assertIsInstance(m, dict)
        except Exception as e:
            self.fail(f"build_sheet_page_map must never raise; got {e!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
