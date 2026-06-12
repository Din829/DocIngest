# -*- coding: utf-8 -*-
"""Unit tests for _xlsx_batch_ground_truth_slice — the per-batch ground-truth
slicer — and _xlsx_per_page_ground_truth, its per-page wrapper. Pure
functions: page→sheet attribution via anchored ranges, with a None / missing
dict entry (= caller sends full markdown) on every uncertainty path."""

from docingest.parsers.base import PAGEBREAK_MARKER
from docingest.pipeline import (
    _docx_per_page_ground_truth,
    _xlsx_batch_ground_truth_slice,
    _xlsx_per_page_ground_truth,
)

VIS = ["A", "B", "C"]
SECS = ["secA", "secB", "secC"]
SEP = f"\n{PAGEBREAK_MARKER}\n"


def test_single_sheet_batch():
    pm = {"A": 1, "B": 5, "C": 8}
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [1, 2, 4], 10) == "secA"


def test_multi_sheet_batch_in_workbook_order():
    pm = {"A": 1, "B": 5, "C": 8}
    out = _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [9, 4, 6], 10)
    assert out == f"secA{SEP}secB{SEP}secC"


def test_last_sheet_owns_through_total_pages():
    pm = {"A": 1, "B": 5, "C": 8}
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [10], 10) == "secC"


def test_unmapped_successor_blocks_owner():
    # B has no bookmark; pages in A's apparent range could belong to B.
    pm = {"A": 1, "C": 8}
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [3], 10) is None


def test_unmapped_successor_at_tail_blocks_last_mapped():
    # C unmapped; B's open-ended range could swallow C's pages.
    pm = {"A": 1, "B": 5}
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [7], 10) is None


def test_mapped_sheet_after_unmapped_is_still_sliceable():
    # B unmapped but the batch only touches C, whose range is sound.
    pm = {"A": 1, "C": 8}
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [9], 10) == "secC"


def test_page_before_first_anchor_returns_none():
    pm = {"B": 5, "C": 8}
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [2], 10) is None


def test_non_monotonic_anchors_return_none():
    pm = {"A": 5, "B": 1, "C": 8}
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, pm, [6], 10) is None


def test_empty_page_map_returns_none():
    assert _xlsx_batch_ground_truth_slice(SECS, VIS, {}, [1], 10) is None


# --- _xlsx_per_page_ground_truth (per-page wrapper) ---

# Bare-marker join so split() round-trips to SECS exactly; the real markdown
# carries newlines around the marker, which ride along harmlessly.
MD = PAGEBREAK_MARKER.join(SECS)


def test_per_page_normal_slicing():
    pm = {"A": 1, "B": 5, "C": 8}
    out = _xlsx_per_page_ground_truth(MD, VIS, pm, [1, 5, 9], 10)
    assert out == {1: "secA", 5: "secB", 9: "secC"}


def test_per_page_doubt_drops_only_that_page():
    # B unmapped: pages in A's apparent range are doubtful, C's are sound.
    pm = {"A": 1, "C": 8}
    out = _xlsx_per_page_ground_truth(MD, VIS, pm, [3, 9], 10)
    assert out == {9: "secC"}


def test_per_page_section_count_mismatch_returns_empty():
    # A hook reshaped the markdown: 2 sections vs 3 visible sheets.
    pm = {"A": 1, "B": 5, "C": 8}
    out = _xlsx_per_page_ground_truth(f"secA{SEP}secB", VIS, pm, [1], 10)
    assert out == {}


def test_per_page_missing_metadata_returns_empty():
    # docx case: no sheet metadata at all → caller keeps the full markdown.
    pm = {"A": 1, "B": 5, "C": 8}
    assert _xlsx_per_page_ground_truth(MD, None, pm, [1], 10) == {}
    assert _xlsx_per_page_ground_truth(MD, VIS, None, [1], 10) == {}
    assert _xlsx_per_page_ground_truth(MD, [], {}, [1], 10) == {}


# --- _docx_per_page_ground_truth (PDF text-layer variant) ---

ANCHOR_P1 = "これは一ページ目の本文でありアンカーとして十分な長さを持つ文章です。"
ANCHOR_P2 = "二ページ目にはこの図の直前に置かれた説明文が存在しています。"

DOCX_MD = "\n".join([
    ANCHOR_P1,
    "",
    "<!-- image: fig1.png -->",
    "",
    "<!-- vision-enriched image=fig1.png -->",
    "図1の転写内容テーブル",
    "",
    ANCHOR_P2,
    "",
    "<!-- image: fig2.png -->",
    "",
    "<!-- vision-enriched image=fig2.png -->",
    "図2の転写内容グラフ",
])

DOCX_PAGES = {
    1: f"{ANCHOR_P1}\nページ1の残りの本文",
    2: f"{ANCHOR_P2}\nページ2の残りの本文",
    3: "",  # scanned page: empty text layer
}


def test_docx_normal_assembly():
    out = _docx_per_page_ground_truth(DOCX_MD, DOCX_PAGES, [1, 2])
    assert "図1の転写内容テーブル" in out[1]
    assert ANCHOR_P1 in out[1]
    assert "図2の転写内容グラフ" not in out[1]   # fig2 belongs to page 2
    assert "図2の転写内容グラフ" in out[2]


def test_docx_scanned_page_keeps_empty_ground_truth():
    # Empty string is meaningful (triggers the full-transcription escape),
    # so the page must be IN the dict, not absent.
    out = _docx_per_page_ground_truth(DOCX_MD, DOCX_PAGES, [3])
    assert out == {3: ""}


def test_docx_ambiguous_anchor_drops_figure_only():
    # Same anchor text on two pages → fig joins no page; page text remains.
    pages = {1: ANCHOR_P1, 2: ANCHOR_P1}
    md = f"{ANCHOR_P1}\n\n<!-- image: fig1.png -->\n\n<!-- vision-enriched image=fig1.png -->\nFIGBLOCK"
    out = _docx_per_page_ground_truth(md, pages, [1, 2])
    assert "FIGBLOCK" not in out[1] and "FIGBLOCK" not in out[2]
    assert ANCHOR_P1 in out[1]


def test_docx_missing_page_texts_returns_empty():
    assert _docx_per_page_ground_truth(DOCX_MD, None, [1]) == {}
    assert _docx_per_page_ground_truth(DOCX_MD, {}, [1]) == {}


def test_docx_page_without_entry_is_skipped():
    # page_no not in page_texts → falls back to full markdown for that page.
    out = _docx_per_page_ground_truth(DOCX_MD, DOCX_PAGES, [1, 99])
    assert 1 in out and 99 not in out
