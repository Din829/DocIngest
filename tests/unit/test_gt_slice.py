# -*- coding: utf-8 -*-
"""Unit tests for _xlsx_batch_ground_truth_slice — the per-batch ground-truth
slicer. Pure function: page→sheet attribution via anchored ranges, with a
None (= caller sends full markdown) on every uncertainty path."""

from docingest.parsers.base import PAGEBREAK_MARKER
from docingest.pipeline import _xlsx_batch_ground_truth_slice

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
