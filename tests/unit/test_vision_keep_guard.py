"""
Unit tests for the vision_keep superset guard (_apply_vision_keep).

Background: vision_keep=vision drops each section's Docling half, assuming the
Vision half is a superset. Docling's PPTX serializer breaks that assumption by
drifting slide boundaries — a divider slide's title or the closing slide's text
lands in a NEIGHBOURING slide's section, and when that slide itself was
triage-skipped there is no Vision fallback: the text used to vanish entirely
(real case: 20260703_KFP pptx lost 6 divider titles + the closing slide).

The guard pins this contract:

  1. Docling half has lines Vision doesn't cover → section keeps BOTH halves
  2. Vision covers everything (modulo NFKC / markdown decoration / reflow)
     → Docling half is dropped as before (dedup benefit preserved)
  3. Short furniture (page numbers, "PwC") never blocks the dedup
  4. The guard also protects auto-mode sections that resolve to "vision"
  5. vision_keep=both stays a no-op

Pure string transformation — no parsing, no network. Fast and offline.

Run:
    python tests/unit/test_vision_keep_guard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docingest.pipeline import _apply_vision_keep, _docling_unique_lines

PB = "<!-- pagebreak -->"


def _cfg(keep: str, min_chars: int = 4) -> dict:
    return {"output": {"vision_keep": keep, "vision_keep_min_unique_chars": min_chars}}


def test_drifted_title_keeps_both() -> None:
    # The real bug shape: a divider title drifted in from the neighbouring
    # slide sits BEFORE the marker; the Vision half never saw it.
    section = (
        "## 2026年6月以降の追加機能\n\n"
        "<!-- vision-enriched -->\n"
        "FreeStyleレイアウトの説明画面。コマンドの利用手順が並ぶ。\n"
    )
    out = _apply_vision_keep(section, _cfg("vision"), "pptx")
    assert "2026年6月以降の追加機能" in out, "drifted title must survive"
    assert "FreeStyleレイアウト" in out, "vision half must survive too"
    print("  ✓ test_drifted_title_keeps_both")


def test_covered_docling_half_still_dropped() -> None:
    # Vision covers every Docling line (despite reflow, emphasis and full-width
    # differences) → superset holds → dedup as before.
    section = (
        "# ＡＩスライド生成の技術トレンド\n\n"
        "画像生成からHTML/CSS変換まで、\n手法が多様化している。\n\n"
        "<!-- vision-enriched -->\n"
        "**AIスライド生成の技術トレンド**\n"
        "画像生成からHTML/CSS変換まで、手法が多様化している。図表: 3系統の比較。\n"
    )
    out = _apply_vision_keep(section, _cfg("vision"), "pptx")
    assert out.startswith("<!-- vision-enriched -->"), "covered Docling half must be dropped"
    print("  ✓ test_covered_docling_half_still_dropped")


def test_furniture_never_blocks_dedup() -> None:
    # Page number + short footer before the marker are not "content" — the
    # guard must not degrade the dedup for them.
    section = (
        "27\n\nPwC\n\n"
        "<!-- vision-enriched -->\n"
        "スライド全体の説明テキスト。\n"
    )
    out = _apply_vision_keep(section, _cfg("vision"), "pptx")
    assert "27" not in out.split("<!--")[0], "furniture-only Docling half must be dropped"
    print("  ✓ test_furniture_never_blocks_dedup")


def test_auto_resolved_vision_is_guarded() -> None:
    # auto resolves clean pages to "vision" — the same guard must apply.
    # Whole-page (page=N) marker so auto participates.
    section = (
        "# Thank you\n\n© [2026] PwC. All rights reserved.\n\n"
        "<!-- vision-enriched page=26 -->\n"
        "Planモードの画面遷移を示すスクリーンショット群の説明。\n"
    )
    out = _apply_vision_keep(section, _cfg("auto"), "pptx")
    assert "Thank you" in out and "rights reserved" in out
    print("  ✓ test_auto_resolved_vision_is_guarded")


def test_keep_both_untouched() -> None:
    doc = f"a\n<!-- vision-enriched -->\nb\n{PB}\nc\n"
    assert _apply_vision_keep(doc, _cfg("both"), "pptx") == doc
    print("  ✓ test_keep_both_untouched")


def test_threshold_is_configurable() -> None:
    # A 5-char unique line blocks dedup at threshold 4 but not at 10.
    section = "独自の五文字\n\n<!-- vision-enriched -->\n別の説明テキスト。\n"
    kept_both = _apply_vision_keep(section, _cfg("vision", min_chars=4), "pptx")
    deduped = _apply_vision_keep(section, _cfg("vision", min_chars=10), "pptx")
    assert "独自の五文字" in kept_both
    assert "独自の五文字" not in deduped
    print("  ✓ test_threshold_is_configurable")


def test_unique_lines_ignores_placeholders() -> None:
    unique = _docling_unique_lines(
        "<!-- image: foo.png -->\n本文にしかない一文\n", "全く別のビジョン説明", 4
    )
    assert unique == ["本文にしかない一文"]
    print("  ✓ test_unique_lines_ignores_placeholders")


if __name__ == "__main__":
    test_drifted_title_keeps_both()
    test_covered_docling_half_still_dropped()
    test_furniture_never_blocks_dedup()
    test_auto_resolved_vision_is_guarded()
    test_keep_both_untouched()
    test_threshold_is_configurable()
    test_unique_lines_ignores_placeholders()
    print("all vision_keep guard tests passed")
