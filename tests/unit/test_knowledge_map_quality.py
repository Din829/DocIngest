"""
Regression guards for knowledge-map / index "representative info" quality —
the section + keyword logic that feeds index.json and knowledge_search.SKILL.md.

These pin behaviours that were each a real bug found while auditing what a
downstream agent reads to search a knowledge base. They are subtle (heading
level, case sensitivity, placeholder words) and easy to silently revert, so
they get explicit tests.

Covers:
  A. _extract_sections — shallowest-heading-level selection, code-fence skip,
     pure-punctuation drop.
  B. keyword extractor — case-insensitive stop matching (the dead-stop-list bug).
  C. knowledge_map keyword body-fallback — fires on empty OR placeholder-only
     title keywords; titled docs untouched.

Run:
    python tests/unit/test_knowledge_map_quality.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.output.index_builder import _extract_sections
from docingest.output.keyword_extractor import create_keyword_extractor
from docingest.output.knowledge_map import build_stage1


# ---------------------------------------------------------------------------
# A. _extract_sections
# ---------------------------------------------------------------------------

def test_sections_shallowest_level_not_always_h2():
    """The main sections are the shallowest heading level present — NOT a fixed
    H2. A deck with H1 chapters and one stray H2 must return the H1 chapters."""
    print("=== test_sections_shallowest_level_not_always_h2 ===")
    md = (
        "# Chapter One\n\nbody\n\n"
        "## a stray subsection\n\nbody\n\n"
        "# Chapter Two\n\nbody\n\n"
        "# Chapter Three\n\nbody\n"
    )
    secs = _extract_sections(md)
    assert secs == ["Chapter One", "Chapter Two", "Chapter Three"], secs
    # And when H2 IS the shallowest (xlsx sheet names — no H1), take the H2s.
    md2 = "## Sheet A\n\nx\n\n## Sheet B\n\ny\n"
    assert _extract_sections(md2) == ["Sheet A", "Sheet B"], _extract_sections(md2)
    print("  shallowest-level selection holds for H1-led and H2-led docs  PASSED\n")


def test_sections_skip_code_fences():
    """A '# comment' inside a ``` / ~~~ code fence is not a heading."""
    print("=== test_sections_skip_code_fences ===")
    md = (
        "# Real Heading\n\n"
        "```python\n# not a heading\n## also not\n```\n\n"
        "~~~\n# fenced too\n~~~\n\n"
        "# Second Real\n"
    )
    assert _extract_sections(md) == ["Real Heading", "Second Real"], _extract_sections(md)
    print("  code-fence contents are not treated as headings  PASSED\n")


def test_sections_drop_pure_punctuation():
    """A heading with zero meaningful chars (letter/digit/CJK) is noise."""
    print("=== test_sections_drop_pure_punctuation ===")
    assert _extract_sections("## !@#$%^&*()") == []
    assert _extract_sections("## ★☆◆◇") == []
    assert _extract_sections("## ---") == []
    # ...but headings that merely CONTAIN punctuation are kept.
    assert _extract_sections("## 4-16. ESG投資とは") == ["4-16. ESG投資とは"]
    assert _extract_sections("## 90%↓ 20%↓") == ["90%↓ 20%↓"]
    assert _extract_sections("## 第1章") == ["第1章"]
    print("  pure-punctuation dropped, punctuation-bearing real titles kept  PASSED\n")


def test_sections_atx_strictness():
    """'#text' without a space and 7+ '#' are not ATX headings."""
    print("=== test_sections_atx_strictness ===")
    assert _extract_sections("#nospace\n# Real\n####### seven") == ["Real"]
    print("  non-ATX lines ignored  PASSED\n")


# ---------------------------------------------------------------------------
# B. keyword extractor — case-insensitive stop matching
# ---------------------------------------------------------------------------

def test_stop_words_case_insensitive():
    """The dead-stop-list bug: title-cased words ("The", "How") must be caught
    by a lowercase stop list. Real content words stay."""
    print("=== test_stop_words_case_insensitive ===")
    cfg = {"knowledge_map": {"keywords": {
        "extra_stop_words": ["the", "how", "all", "figure"],
        "latin_min_len": 2,  # so 2-letter test words aren't length-filtered
    }}}
    ext = create_keyword_extractor(cfg)
    # Title-cased stop words → dropped regardless of case.
    for noise in ["The", "THE", "How", "All", "Figure"]:
        assert ext.extract(noise) == [], f"{noise!r} should be a stop word"
    # Real content words → kept.
    for real in ["CSR", "Git", "UNIQLO", "Energy"]:
        assert ext.extract(real) == [real], f"{real!r} should survive"
    print("  stop matching is case-insensitive; content preserved  PASSED\n")


# ---------------------------------------------------------------------------
# C. knowledge_map keyword body-fallback
# ---------------------------------------------------------------------------

def _km(index_files, chunks, extra_cfg=None):
    cfg = {
        "knowledge_map": {"keywords": {
            "extra_stop_words": ["transcript", "the", "and"],
            "max_per_file": 15, "max_index": 50,
            "fallback_to_body": True, "fallback_max_chunks": 5,
            "fallback_max_kw_len": 12,
        }},
    }
    if extra_cfg:
        cfg["knowledge_map"]["keywords"].update(extra_cfg)
    index_data = {"files": index_files}
    return build_stage1(index_data, chunks, cfg)


def test_keyword_fallback_on_placeholder_title():
    """An audio transcript whose only section is the placeholder "Transcript"
    must fall back to body keywords (real content), not keep "Transcript"."""
    print("=== test_keyword_fallback_on_placeholder_title ===")
    index_files = [{
        "path": "sources/rec.md", "original_file": "rec.m4a",
        "format": "m4a", "language": "ja", "sections": ["Transcript"],
        "chunks_count": 1,
    }]
    chunks = [{
        "id": "c1",
        "text": "## Transcript\n\n仕事のキャリア相談です。健康と結婚について話しました。",
        "metadata": {"source": "sources/rec.md"},
    }]
    km = _km(index_files, chunks)
    kws = km["files"][0].get("keywords", [])
    assert "Transcript" not in kws, kws
    assert any(w in kws for w in ["仕事", "キャリア", "健康", "結婚", "相談"]), kws
    print(f"  placeholder title fell back to body keywords: {kws[:6]}  PASSED\n")


def test_keyword_no_fallback_when_titled():
    """A document with real section titles keeps title-derived keywords and does
    NOT pull in body words — fallback only fires for structure-less docs."""
    print("=== test_keyword_no_fallback_when_titled ===")
    index_files = [{
        "path": "sources/db.md", "original_file": "db.xlsx",
        "format": "xlsx", "language": "en",
        "sections": ["products", "orders", "customers"],
        "chunks_count": 1,
    }]
    chunks = [{
        "id": "c1",
        "text": "## products\n\nlorem ipsum dolor sit amet body filler words",
        "metadata": {"source": "sources/db.md"},
    }]
    km = _km(index_files, chunks)
    kws = km["files"][0].get("keywords", [])
    assert "products" in kws and "orders" in kws, kws
    # Body filler ("lorem", "ipsum", ...) must NOT appear — fallback didn't fire.
    assert not any(w in kws for w in ["lorem", "ipsum", "dolor", "filler"]), kws
    print(f"  titled doc keeps title keywords, no body leakage: {kws}  PASSED\n")


def main():
    test_sections_shallowest_level_not_always_h2()
    test_sections_skip_code_fences()
    test_sections_drop_pure_punctuation()
    test_sections_atx_strictness()
    test_stop_words_case_insensitive()
    test_keyword_fallback_on_placeholder_title()
    test_keyword_no_fallback_when_titled()
    print("ALL knowledge-map-quality TESTS PASSED")


if __name__ == "__main__":
    main()
