"""
Table-cell number-fragment detection (quality_report).

Guards the two properties that make this check worth having at all:
  * it fires on the real damage shape (a column boundary cutting numbers)
  * it stays silent on legitimate content that LOOKS similar — above all the
    space thousands separator (`1 494`), which IEA/EU-style documents use
    everywhere. Without that exclusion the check reports 3 false positives on
    our own IEA artefact; with it, 0.

Real-world provenance: the damaged rows below are verbatim output from an
alternative Rust PDF engine evaluated against IEA WEO table pages, where
`571` came out as `|71 5|` and `89` as `|9 86|8|`. The clean rows are from
our own produced artefacts.
"""

from docingest.output.quality_report import (
    _is_number_fragment,
    scan_number_fragments,
    generate_report,
    format_summary,
)


def test_fragment_shapes_detected():
    """The measured damage shapes must all be caught."""
    print("=== test_fragment_shapes_detected ===")
    for cell in ["71 5", "9 86", ".4 0.5", ".3 0.5", "74 3", "23 2", ".4"]:
        assert _is_number_fragment(cell), f"missed fragment: {cell!r}"
        print(f"  caught: {cell!r}")


def test_space_thousands_not_flagged():
    """`1 494` style numbers are legitimate, not damage. The critical guard."""
    print("=== test_space_thousands_not_flagged ===")
    for cell in ["1 494", "8 091", "1 000", "1 500", "2 434", "12 345", "1 234 567"]:
        assert not _is_number_fragment(cell), f"false positive: {cell!r}"
        print(f"  ok (not flagged): {cell!r}")


def test_ordinary_cells_not_flagged():
    print("=== test_ordinary_cells_not_flagged ===")
    for cell in ["", "0.9", "513", "89", "North America", "2024", "45.2",
                 "12", "1", "N/A", "2000-24", "Jan 2024", "-0.1"]:
        assert not _is_number_fragment(cell), f"false positive: {cell!r}"
    print("  13 ordinary cells: none flagged")


def test_scan_real_damaged_table():
    """End-to-end on the verbatim damaged row vs its correct counterpart."""
    print("=== test_scan_real_damaged_table ===")
    damaged = (
        "|Region|2000-24|2024-35|2024-50|2024|2035|2050|\n"
        "|---|---|---|---|---|---|---|\n"
        "|North America|0.9|.4 0.5|0|513|543|71 5|\n"
        "|United States|0.7|.4 0.4|0|340|357|74 3|\n"
    )
    correct = (
        "|Region|2000-24|2024-35|2024-50|2024|2035|2050|\n"
        "|---|---|---|---|---|---|---|\n"
        "|North America|0.9|0.5|0.4|513|543|571|\n"
        "|United States|0.7|0.4|0.4|340|357|374|\n"
    )
    dmg = scan_number_fragments(damaged)
    ok = scan_number_fragments(correct)
    print(f"  damaged -> {dmg['count']} fragments {[s['cell'] for s in dmg['samples']]}")
    print(f"  correct -> {ok['count']} fragments")
    assert dmg["count"] == 4, dmg
    assert ok["count"] == 0, ok
    assert dmg["samples"][0]["line"] == 3          # 1-indexed, skips separator
    assert dmg["samples"][0]["cell"] == ".4 0.5"


def test_iea_style_table_is_clean():
    """A real IEA-shaped table (space thousands + axis ticks) must be silent."""
    print("=== test_iea_style_table_is_clean ===")
    iea = (
        "| | 0 | 500 | 1 000 | 1 500 |\n"
        "|---|---|---|---|---|\n"
        "|Africa|2.5|2.1|1 494|1 884|\n"
        "|World|1.2|0.8|8 091|9 572|\n"
    )
    result = scan_number_fragments(iea)
    print(f"  fragments: {result['count']}")
    assert result["count"] == 0, result["samples"]


def test_non_table_text_ignored():
    """Prose containing similar-looking sequences must not be scanned."""
    print("=== test_non_table_text_ignored ===")
    prose = "The range was 2.5-3 °C, up from .4 0.5 in earlier drafts.\n"
    assert scan_number_fragments(prose)["count"] == 0
    print("  prose ignored (only pipe-rows are scanned)")


def test_report_keeps_fragments_out_of_quality_score(tmp_path):
    """
    A file with split numbers but zero Vision markers must still score 1.0 —
    the two dimensions stay separate — while being reported and named loudly.
    """
    print("=== test_report_keeps_fragments_out_of_quality_score ===")
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "a.md").write_text(
        "|Region|2050|\n|---|---|\n|North America|71 5|\n",
        encoding="utf-8",
    )
    report = generate_report(sources)
    print(f"  quality_score={report['quality_score']} "
          f"fragments={report['total_number_fragments']}")
    assert report["total_number_fragments"] == 1
    assert report["quality_score"] == 1.0          # untouched by fragments
    assert report["files_with_issues"] == 1        # but the file IS surfaced

    summary = format_summary(report)
    print(f"  summary: {summary}")
    assert "WARNING" in summary and "split numbers" in summary
    # The old wording would have called this run "clean" — make sure it cannot.
    assert not summary.endswith("zero uncertainty markers)")


def test_clean_report_unchanged(tmp_path):
    """No fragments → summary keeps its original clean wording."""
    print("=== test_clean_report_unchanged ===")
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "a.md").write_text("# Title\n\nPlain text.\n", encoding="utf-8")
    report = generate_report(sources)
    summary = format_summary(report)
    print(f"  summary: {summary}")
    assert report["total_number_fragments"] == 0
    assert "WARNING" not in summary
    assert "clean" in summary


if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path

    test_fragment_shapes_detected()
    test_space_thousands_not_flagged()
    test_ordinary_cells_not_flagged()
    test_scan_real_damaged_table()
    test_iea_style_table_is_clean()
    test_non_table_text_ignored()
    with tempfile.TemporaryDirectory() as d:
        test_report_keeps_fragments_out_of_quality_score(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_clean_report_unchanged(Path(d))
    print("\nAll number-fragment tests passed.")
