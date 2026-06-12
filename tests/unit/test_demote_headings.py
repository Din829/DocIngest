# -*- coding: utf-8 -*-
"""Unit tests for _demote_headings — Vision supplement/figure blocks must not
inject structural headings into the document (title_path pollution)."""

from docingest.pipeline import _demote_headings


def test_headings_become_bold():
    src = "# Chart Title\n\n### 凡例\n\nbody line"
    out = _demote_headings(src)
    assert out == "**Chart Title**\n\n**凡例**\n\nbody line"


def test_all_levels_demoted():
    src = "\n".join(f"{'#' * n} L{n}" for n in range(1, 7))
    out = _demote_headings(src)
    assert "#" not in out
    assert "**L1**" in out and "**L6**" in out


def test_fenced_code_untouched():
    src = "### title\n```python\n# a comment, not a heading\n```\n## after"
    out = _demote_headings(src)
    assert "# a comment, not a heading" in out
    assert "**title**" in out and "**after**" in out


def test_non_heading_hash_lines_untouched():
    # No space after # / mid-line # → not a heading, leave alone.
    src = "#hashtag\nvalue # comment\n|cell|#1|"
    assert _demote_headings(src) == src


def test_table_and_bold_untouched():
    src = "| A | B |\n| - | - |\n**already bold**"
    assert _demote_headings(src) == src
