"""
Excel denoising — empty/placeholder row removal.

Regression suite for the bug where Docling renders an Excel sheet's blown-up
used-range (1M+ rows) as `|  | -- |  |  |` placeholder rows, and the old
separator regex `^\\s*\\|[-:\\s|]+\\|\\s*$` mis-classified them as Markdown
separators — protecting 1.05M noise rows from cleanup (31.6 MB of garbage).

The fix has two invariants, asserted below:
  1. ZERO false deletion (first principle): any row holding a letter, digit, or
     symbol-mark (○ ✓ × ● ★ …) is NEVER removed. Losing a checklist mark is an
     information accident and must never happen.
  2. Placeholder rows built only from dashes / spaces / zero-width / format
     chars (in ANY combination, half- or full-width) ARE removed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.pipeline import (
    _cell_is_empty,
    _is_separator_row,
    _clean_excel_markdown,
)

# Minimal config: denoising on, both passes on (mirrors default.yaml).
_CFG = {"parsing": {"xlsx": {"denoising": {
    "enabled": True, "dedup_cells": True, "strip_empty_cells": True,
}}}}


# ── _cell_is_empty: the core judgement ─────────────────────────────────────

# Cells that carry NO information → empty (dashes/space/zero-width/placeholder).
EMPTY_CELLS = [
    "", "  ", "\t", "　",          # blank, spaces, tab, ideographic space
    "-", "--", "---", "–", "—",     # ascii dashes / en / em
    "―", "－", "‐",                  # horizontal bar (Pd), fullwidth hyphen (Pd), hyphen (Pd)
    "_", "__",                       # connector underscores (Pc) → empty
    "​", "\xa0",                # zero-width space, NBSP
    "None", "none",                  # Docling artifact
    "- - -", " -- ",                 # mixed dash + space
]

# Cells that DO carry information → never empty (first-principle protection).
CONTENT_CELLS = [
    "○", "◯", "●", "★", "☆", "✓", "✗", "×", "✕", "◎", "△", "▲",  # checklist marks
    "░", "▒", "▓", "♥", "♪", "▪", "◦",                            # decorative (kept, conservative)
    "0", "-1", "100%", "①", "②", "Ⅲ", "Ⅳ",                       # numbers / circled / roman
    "¥100", "$50", "H2O", "25℃",                                  # currency / formula / unit
    "あ", "A", "未定", "n/a", "N/A", "TBD", "該当なし",             # text / semantic null (kept)
    "2020-2025", "A-1",                                            # dash *between* content
    # ー (U+30FC) is a real katakana long-vowel mark (コーヒー), Unicode class Lm
    # — NOT a dash. First principle: keep it. A row that is genuinely all-ー is
    # rare; deleting it would risk dropping rows holding real long-vowel words.
    "ー", "コーヒー",
    # Dot-family fillers are category Po — deliberately NOT in the empty set
    # (a lone `.` may be a decimal remnant). Kept as content by design.
    ".", "...", "·", "。",
]


def test_cell_is_empty_true_for_noise():
    for c in EMPTY_CELLS:
        assert _cell_is_empty(c), f"expected EMPTY but got content: {c!r}"


def test_cell_is_empty_false_for_content():
    for c in CONTENT_CELLS:
        assert not _cell_is_empty(c), f"expected CONTENT but got empty: {c!r}"


# ── _is_separator_row: only genuine ≥3-dash separators ─────────────────────

def test_real_separators_recognised():
    for sep in ["| --- |", "| --- | --- |", "|:---|---:|", "| :---: |", "|----|"]:
        assert _is_separator_row(sep), f"should be separator: {sep!r}"


def test_placeholder_rows_are_not_separators():
    # The exact bug shape + variants: a stray short dash among blanks must NOT
    # read as a separator, or it gets protected from cleanup.
    for ph in ["|  | -- |  |  |", "|  | - |  |", "| - | - |", "|  |  |  |"]:
        assert not _is_separator_row(ph), f"should NOT be separator: {ph!r}"


# ── _clean_excel_markdown: end-to-end row removal ──────────────────────────

def test_million_placeholder_rows_removed():
    """The reproduction: many `|  | -- |  |  |` rows collapse, content survives."""
    md = (
        "| Code | Role |\n"
        "| --- | --- |\n"
        "| A1 | Partner |\n"
        + "|  | -- |  |  |  |  |\n" * 500   # the noise
        + "| A2 | Director |\n"
    )
    out = _clean_excel_markdown(md, _CFG)
    assert "Partner" in out and "Director" in out      # content kept
    assert "A1" in out and "A2" in out
    assert out.count("-- |  |") == 0                    # placeholder rows gone
    # No 500-row block remains:
    assert out.count("\n") < 20


def test_checklist_marks_never_deleted():
    """First principle: a row of yes/no marks must survive denoising intact."""
    md = (
        "| # | 部門A | 部門B | 部門C |\n"
        "| --- | --- | --- | --- |\n"
        "| 1 | ○ | × | ○ |\n"
        "| 2 | ○ | ○ | × |\n"
    )
    out = _clean_excel_markdown(md, _CFG)
    assert out.count("○") == 4, f"lost ○ marks:\n{out}"
    assert out.count("×") == 2, f"lost × marks:\n{out}"


def test_sparse_row_strips_placeholders_keeps_value():
    """A predominantly-empty row keeps its one real value, drops dash fillers."""
    md = "| 画面ID |  | -- | A15S010 |  | -- |\n"
    out = _clean_excel_markdown(md, _CFG)
    assert "画面ID" in out and "A15S010" in out
    assert "--" not in out


def test_separator_row_preserved():
    md = "| Code | Role |\n| --- | --- |\n| A1 | Partner |\n"
    out = _clean_excel_markdown(md, _CFG)
    assert "| --- | --- |" in out  # genuine separator untouched


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all passed")
