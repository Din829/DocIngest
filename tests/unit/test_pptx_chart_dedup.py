# -*- coding: utf-8 -*-
"""PPTX chart hook dedup — no double data when docling extracts natively,
and NO silent data loss from over-eager matching.

docling >= 2.113 parses native PPTX charts itself; before this guard the
pptx_chart hook re-injected the same data table (found on a real file the
day 2.113 landed — 404 unit tests were green while the actual artefact
carried every chart twice). The matcher is deliberately biased toward
injecting: adversarial cases below pin the two data-loss traps (duplicate
values inside one row collapsing under set semantics; a small chart
colliding with an unrelated background table).

Run:
    python -m pytest tests/unit/test_pptx_chart_dedup.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.hooks.pptx_chart import (
    _chart_already_present,
    _table_row_multisets,
)

HOOK_CHART = """### Chart: FY2025 Revenue
> type: COLUMN_CLUSTERED

| Category | Product A | Product B |
|---|---|---|
| Q1 | 10.5 | 8.2 |
| Q2 | 12.1 | 9 |
"""

# docling-style rendering: aligned padding, EMPTY first header cell
DOCLING_TABLE = """FY2025 Revenue

|    |   Product A |   Product B |
|----|-------------|-------------|
| Q1 |        10.5 |         8.2 |
| Q2 |        12.1 |           9 |
"""


def test_present_despite_formatting_differences():
    rows = _table_row_multisets(DOCLING_TABLE)
    assert _chart_already_present(HOOK_CHART, rows), (
        "aligned padding / different header must not defeat the matcher"
    )


def test_absent_when_markdown_has_no_chart_data():
    rows = _table_row_multisets("# A slide\n\nJust text, no tables.\n")
    assert not _chart_already_present(HOOK_CHART, rows)


def test_partial_extraction_still_injects():
    # Only Q1 present — missing rows mean docling's extraction is incomplete,
    # so the hook must still inject (no data loss).
    partial = "|    |   Product A |   Product B |\n|--|--|--|\n| Q1 |  10.5 | 8.2 |\n"
    rows = _table_row_multisets(partial)
    assert not _chart_already_present(HOOK_CHART, rows)


def test_subset_match_tolerates_extra_docling_columns():
    wider = (
        "|    | Product A | Product B | Total |\n|--|--|--|--|\n"
        "| Q1 | 10.5 | 8.2 | 18.7 |\n| Q2 | 12.1 | 9 | 21.1 |\n"
    )
    rows = _table_row_multisets(wider)
    assert _chart_already_present(HOOK_CHART, rows)


# --- adversarial: the two data-loss traps -------------------------------

def test_duplicate_values_in_one_row_not_collapsed():
    """| Q1 | 8 | 8 | must NOT match a background row | Q1 | 8 | 3 | —
    set semantics would collapse the two 8s and silently drop the chart."""
    chart = (
        "### Chart\n\n| Category | A | B |\n|---|---|---|\n"
        "| Q1 | 8 | 8 |\n| Q2 | 5 | 5 |\n"
    )
    background = (
        "| X | Y | Z |\n|--|--|--|\n| Q1 | 8 | 3 |\n| Q2 | 5 | 1 |\n"
    )
    rows = _table_row_multisets(background)
    assert not _chart_already_present(chart, rows)

    # But a REAL native extraction (both 8s present) does match.
    native = "|    | A | B |\n|--|--|--|\n| Q1 | 8 | 8 |\n| Q2 | 5 | 5 |\n"
    assert _chart_already_present(chart, _table_row_multisets(native))


def test_single_row_chart_always_injects():
    """One small row of common values can collide with any unrelated table —
    a single data row is too little evidence, so the hook must inject even
    when that row appears verbatim in the markdown."""
    chart = "### Chart\n\n| Category | Value |\n|---|---|\n| Total | 100 |\n"
    background = "| Item | Total | 100 |\n|--|--|--|\n| Total | 100 | ok |\n"
    rows = _table_row_multisets(background)
    assert not _chart_already_present(chart, rows)


def test_common_numbers_background_table_not_confused():
    """A financial table that merely SHARES some numbers with the chart must
    not suppress injection unless every data row is genuinely covered."""
    chart = (
        "### Chart\n\n| Category | A | B |\n|---|---|---|\n"
        "| 2024 | 10 | 20 |\n| 2025 | 30 | 40 |\n"
    )
    background = (
        "| Year | Revenue | Cost |\n|--|--|--|\n"
        "| 2024 | 10 | 20 |\n| 2025 | 99 | 40 |\n"  # 2025 row differs
    )
    rows = _table_row_multisets(background)
    assert not _chart_already_present(chart, rows)


# --- e2e ----------------------------------------------------------------

def test_e2e_no_duplicate_chart_on_real_fixture():
    """Parse the real test_chart.pptx with the CURRENT docling, run the hook,
    and assert each chart data row appears exactly once in the final markdown."""
    from docingest.config import load_config
    from docingest.hooks import run_post_parse_hooks
    from docingest.parsers.docling_parser import DoclingParser

    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "test_chart.pptx"
    config = load_config()
    parser = DoclingParser(config)
    result = parser.parse(fixture)
    assert result.success

    run_post_parse_hooks(fixture, result, config, phase="post_parse")

    # The fixture's chart has a Q3 row with the value 14.3 — count table rows
    # containing both. Exactly one, regardless of who extracted it.
    q3_rows = [
        line for line in result.markdown.splitlines()
        if line.strip().startswith("|") and "Q3" in line and "14.3" in line
    ]
    assert len(q3_rows) == 1, (
        f"expected exactly one Q3 data row, got {len(q3_rows)}:\n"
        + "\n".join(q3_rows)
    )
