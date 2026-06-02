"""
Test refine large-file splitting (docingest.refine._split_for_refine).

Pure-logic tests — exercises the heading-aligned splitter without any LLM
call, so it needs no API key and runs in milliseconds. The end-to-end
splitting+stitching with a real model is verified separately (it burns tokens);
these tests guard the split invariants that make that path correct:

  - oversized input → multiple pieces, each under the per-call ceiling
  - tables are NEVER split across pieces (cuts avoid protected spans)
  - no content is lost when pieces are concatenated back
  - small input → a single piece (the original single-call path is untouched)

Run:
    python tests/unit/test_refine_split.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.refine import (
    _split_for_refine,
    _refine_pieces,
    estimate_refine_cost,
    check_refine_budget,
)
from docingest.chunkers.base import BaseChunker
from docingest.config import load_config


def _make_doc(sections: int, rows_per_table: int) -> str:
    """Build a markdown doc with `sections` headings, each holding a table."""
    parts = []
    for i in range(sections):
        parts.append(f"## セクション {i}")
        parts.append(f"説明文 {i}。" * 20)  # some prose to grow tokens
        parts.append("| 項目 | 値 |")
        parts.append("| :--- | :--- |")
        for r in range(rows_per_table):
            parts.append(f"| 行{i}-{r} | データ{i}-{r} |")
        parts.append("")
    return "\n".join(parts)


def _table_rows(s: str) -> int:
    return sum(1 for ln in s.splitlines()
               if ln.strip().startswith("|") and ln.strip().endswith("|"))


def test_small_doc_single_piece():
    """A doc under target stays one piece — small-file path unchanged."""
    doc = _make_doc(sections=2, rows_per_table=3)
    pieces = _split_for_refine(doc, target_tokens=8000)
    assert len(pieces) == 1, f"small doc should be 1 piece, got {len(pieces)}"
    print("PASS: small doc → single piece")


def test_large_doc_splits_into_multiple():
    """A large doc splits into several pieces, each under the call ceiling."""
    doc = _make_doc(sections=40, rows_per_table=10)
    total = BaseChunker.estimate_tokens(doc)
    pieces = _split_for_refine(doc, target_tokens=2000)
    assert len(pieces) > 1, f"large doc should split, got {len(pieces)} pieces"
    # Every piece must fit comfortably under a typical max_input ceiling.
    for i, p in enumerate(pieces):
        tok = BaseChunker.estimate_tokens(p)
        assert tok < 50000, f"piece {i} too big: {tok}"
    print(f"PASS: large doc ({total:,} tok) → {len(pieces)} pieces, all < ceiling")


def test_tables_never_split():
    """Total table rows across pieces == source: no table row lost or split."""
    doc = _make_doc(sections=30, rows_per_table=12)
    src_rows = _table_rows(doc)
    pieces = _split_for_refine(doc, target_tokens=2000)
    piece_rows = sum(_table_rows(p) for p in pieces)
    assert piece_rows == src_rows, (
        f"table rows changed: source={src_rows}, pieces={piece_rows} "
        f"(a table was split across a cut)"
    )
    print(f"PASS: tables intact ({src_rows} rows preserved across {len(pieces)} pieces)")


def test_no_content_lost():
    """Non-blank lines are preserved when pieces are concatenated back."""
    doc = _make_doc(sections=25, rows_per_table=8)
    pieces = _split_for_refine(doc, target_tokens=2000)

    def nonblank(s: str) -> list[str]:
        return [ln.rstrip() for ln in s.split("\n") if ln.strip()]

    src = nonblank(doc)
    merged = nonblank("\n\n".join(pieces))
    assert len(merged) == len(src), (
        f"content lost: source={len(src)} non-blank lines, merged={len(merged)}"
    )
    print(f"PASS: no content lost ({len(src)} non-blank lines preserved)")


def test_cuts_on_heading_boundaries():
    """Every piece after the first starts at a heading."""
    doc = _make_doc(sections=30, rows_per_table=10)
    pieces = _split_for_refine(doc, target_tokens=2000)
    for i, p in enumerate(pieces[1:], start=1):
        assert p.lstrip().startswith("#"), (
            f"piece {i} does not start at a heading (cut mid-section)"
        )
    print(f"PASS: all {len(pieces)-1} cuts on heading boundaries")


def test_refine_pieces_empty_falls_back_to_original():
    """A piece whose model output is empty keeps its original text (no drop).

    Uses a stub model_config path is not possible without an LLM, so we test
    the fallback contract directly by monkeypatching text_completion.
    """
    import docingest.refine as refine_mod

    pieces = ["## A\ncontent A", "## B\ncontent B"]
    calls = {"n": 0}

    def fake_tc(prompt, system_prompt="", model_config=None, max_tokens=None):
        calls["n"] += 1
        # Second piece returns empty → must fall back to original.
        if "content B" in prompt:
            return "", "stop"
        return "REFINED: " + prompt, "stop"

    orig = refine_mod.text_completion
    refine_mod.text_completion = fake_tc
    try:
        stitched, trunc = _refine_pieces(
            pieces, system_prompt="x", model_config={}, max_output=1000,
            parallel=False, max_workers=1,
        )
    finally:
        refine_mod.text_completion = orig

    assert "REFINED: ## A" in stitched, "piece A should be refined"
    assert "content B" in stitched, "empty-output piece B must keep original text"
    assert trunc is False
    print("PASS: empty piece output falls back to original (no content drop)")


def test_cost_estimate_counts_pieces():
    """Cost estimate computes pieces per file and a non-negative dollar figure."""
    cfg = load_config()
    est = estimate_refine_cost([("big.md", 101784), ("small.md", 5000)], cfg)
    # big file (>50k) splits; small file (<50k) stays one piece.
    assert est["per_file"][0]["pieces"] > 1, "big file should split into >1 piece"
    assert est["per_file"][1]["pieces"] == 1, "small file should be 1 piece"
    assert est["total_pieces"] == est["per_file"][0]["pieces"] + 1
    assert est["est_cost_usd"] >= 0.0
    print(f"PASS: cost estimate ({est['total_pieces']} pieces, ${est['est_cost_usd']})")


def test_cost_gate_three_modes():
    """off → ok always; warn → warn over budget; strict → block over budget."""
    cfg_base = load_config()
    est = estimate_refine_cost([("big.md", 101784)], cfg_base)

    for mode, expected in [("off", "ok"), ("warn", "warn"), ("strict", "block")]:
        cfg = load_config(cli_overrides={
            "refine": {"cost_check": {"mode": mode, "max_pieces": 1}}
        })
        action, _ = check_refine_budget(est, cfg)
        assert action == expected, f"mode={mode}: expected {expected}, got {action}"

    # Under budget (high thresholds) → ok even in strict.
    cfg_ok = load_config(cli_overrides={
        "refine": {"cost_check": {"mode": "strict", "max_pieces": 99999,
                                  "max_est_cost_usd": 99999}}
    })
    action, _ = check_refine_budget(est, cfg_ok)
    assert action == "ok", f"under-budget strict should be ok, got {action}"
    print("PASS: cost gate off/warn/strict + under-budget all correct")


def main():
    test_small_doc_single_piece()
    test_large_doc_splits_into_multiple()
    test_tables_never_split()
    test_no_content_lost()
    test_cuts_on_heading_boundaries()
    test_refine_pieces_empty_falls_back_to_original()
    test_cost_estimate_counts_pieces()
    test_cost_gate_three_modes()
    print("ALL refine-split TESTS PASSED")


if __name__ == "__main__":
    main()
