"""
vision_keep="auto" — per-page half selection (offline, no Vision/network).

Verifies the new fourth mode picks the right half per page from on-page signals,
and crucially that it NEVER does worse than "vision" on a silent page, and that
it leaves `image=` supplement markers untouched.

Run:
    python tests/unit/test_vision_keep_auto.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.pipeline import (
    _apply_vision_keep,
    _page_prefers_vision,
    _looks_table_collapsed,
)

PB = "<!-- pagebreak -->"
VMARK = "<!-- vision-enriched page={n} -->"


def _section(docling: str, vision: str, n: int = 1) -> str:
    return f"{docling}\n{VMARK.format(n=n)}\n{vision}"


# --- _looks_table_collapsed ------------------------------------------------

def test_collapsed_table_detected() -> None:
    collapsed = "\n".join([
        "| 2024 | 5000 | 30 |",
        "| 2025 | 4700 | 28 |",
        "| 2026 | 4500 | 25 |",
        "| 2027 | 4200 | 22 |",
    ])
    assert _looks_table_collapsed(collapsed) is True
    print("ok: collapsed bare-number table detected")


def test_real_table_not_flagged() -> None:
    real = "\n".join([
        "| Region | Capital cost | Notes |",
        "| --- | --- | --- |",
        "| North America | 5000 USD | high demand |",
        "| Western Europe | 4700 USD | stable market |",
    ])
    # rows have multi-word cells → not collapsed
    assert _looks_table_collapsed(real) is False
    print("ok: real labelled table not flagged collapsed")


def test_prose_not_flagged() -> None:
    assert _looks_table_collapsed("Just a paragraph of normal text.") is False
    print("ok: prose not flagged collapsed")


# --- _page_prefers_vision --------------------------------------------------

def test_vision_riddled_keeps_docling() -> None:
    docling = "Clean docling text for this page."
    vision = "Header [unreadable: blur] body [unreadable: cut] tail [?] more [unreadable]"
    # 3+ markers in vision, far more than docling → keep Docling
    assert _page_prefers_vision(docling, vision) is False
    print("ok: vision riddled with markers → keep docling")


def test_docling_collapsed_keeps_vision() -> None:
    docling = "\n".join([
        "| 2024 | 5000 | 30 |", "| 2025 | 4700 | 28 |",
        "| 2026 | 4500 | 25 |", "| 2027 | 4200 | 22 |",
    ])
    vision = "| Year | LCOE | Capacity |\n| --- | --- | --- |\n| 2024 | 5000 USD | 30 GW |"
    assert _page_prefers_vision(docling, vision) is True
    print("ok: docling collapsed + vision sound → keep vision")


def test_silent_page_keeps_vision() -> None:
    # No strong signal either way → default bias = vision (never worse than mode "vision")
    assert _page_prefers_vision("normal docling", "normal vision text") is True
    print("ok: silent page → keep vision (default bias)")


# --- _apply_vision_keep with keep=auto -------------------------------------

def _cfg(keep: str) -> dict:
    return {"output": {"vision_keep": keep}}


def test_auto_mixed_document() -> None:
    # Page 1: vision riddled → keep docling. Page 2: docling collapsed → keep vision.
    p1 = _section(
        "Docling page one clean text.",
        "[unreadable: a] [unreadable: b] [unreadable: c] [?]",
        n=1,
    )
    collapsed = "\n".join(["| 2024 | 5000 | 30 |", "| 2025 | 4700 | 28 |",
                           "| 2026 | 4500 | 25 |", "| 2027 | 4200 | 22 |"])
    p2 = _section(collapsed, "| Year | LCOE |\n| --- | --- |\n| 2024 | 5000 USD |", n=2)
    md = p1 + PB + p2

    out = _apply_vision_keep(md, _cfg("auto"), "pdf")
    # page 1 kept docling → docling text present, its vision markers gone
    assert "Docling page one clean text." in out
    assert "[unreadable: a]" not in out
    # page 2 kept vision → the labelled vision table present, collapsed gone
    assert "LCOE" in out
    assert "| 2024 | 5000 | 30 |" not in out
    print("ok: auto picks docling on p1, vision on p2")


def test_auto_leaves_image_supplement_untouched() -> None:
    # image= supplement marker: there is no docling-half to drop; auto keeps all.
    section = "Page text.\n<!-- vision-enriched image=fig1.png -->\nA chart of sales."
    out = _apply_vision_keep(section, _cfg("auto"), "pdf")
    assert "Page text." in out
    assert "A chart of sales." in out
    assert "image=fig1.png" in out
    print("ok: auto leaves image= supplement marker untouched")


def test_auto_protects_xlsx_and_docx() -> None:
    section = _section("docling body", "vision view", n=1)
    # xlsx Guard keys off parsing.xlsx.vision.supplement_only (real config has it
    # =true); supply it so the supplement-mode guard fires as in production.
    xlsx_cfg = {
        "output": {"vision_keep": "auto"},
        "parsing": {"xlsx": {"vision": {"supplement_only": True}}},
    }
    assert _apply_vision_keep(section, xlsx_cfg, "xlsx") == section
    # docx (Mode B) guard is format-based, no config needed.
    assert _apply_vision_keep(section, _cfg("auto"), "docx") == section
    print("ok: auto still forces both for xlsx/docx (safety guards intact)")


def test_existing_modes_unchanged() -> None:
    section = _section("DOC", "VIS", n=1)
    # both = no-op
    assert _apply_vision_keep(section, _cfg("both"), "pdf") == section
    # vision drops docling
    v = _apply_vision_keep(section, _cfg("vision"), "pdf")
    assert "VIS" in v and "DOC" not in v
    # docling drops vision
    d = _apply_vision_keep(section, _cfg("docling"), "pdf")
    assert "DOC" in d and "VIS" not in d
    print("ok: both/vision/docling modes unchanged (no regression)")


if __name__ == "__main__":
    test_collapsed_table_detected()
    test_real_table_not_flagged()
    test_prose_not_flagged()
    test_vision_riddled_keeps_docling()
    test_docling_collapsed_keeps_vision()
    test_silent_page_keeps_vision()
    test_auto_mixed_document()
    test_auto_leaves_image_supplement_untouched()
    test_auto_protects_xlsx_and_docx()
    test_existing_modes_unchanged()
    print("\nALL VISION_KEEP AUTO TESTS PASSED")
