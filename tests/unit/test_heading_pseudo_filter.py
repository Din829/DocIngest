"""
Tests for HeadingChunker's pseudo-heading filter.

Docling mislabels list items / figure captions / whole sentences as ##
headings when a PDF renders them slightly larger; those poison title_path.
The filter demotes such #-lines to body text. Fixtures use REAL mislabeled
headings observed in 総務省_実証システム詳細設計.pdf plus JA/EN/ZH titles to
prove the rules are language-agnostic (the whole point — no per-document
tuning).
"""

from __future__ import annotations

from docingest.chunkers.heading import HeadingChunker


def _chunker(enabled=True):
    return HeadingChunker({"chunking": {
        "max_tokens": 512, "min_tokens": 100,
        "heading": {"filter_pseudo_headings": {"enabled": enabled}},
    }})


# ---- _looks_like_real_heading: the core judgement ----

REAL_HEADINGS = [
    # JA spec numbering
    "1. ICT リテラシー育成システムの概要と特長",
    "2.1. 権限", "3.5.1. 属性作成・変更",
    # EN — including legitimately long titles (no length rule!)
    "Introduction", "Executive Summary", "1. Background",
    "Chapter 4: Results and Discussion",
    "Behavioral Insights in Financial Education",
    "Key Findings from the 2025 Investor Survey",
    # ZH
    "第一章 总则", "二、参赛对象", "3.2 数据处理流程", "项目背景与目标",
]

PSEUDO_HEADINGS = [
    # list items (real Docling mislabels)
    "・  入力ログイン ID",
    "1) [ アナウンス ] 欄",
    "- Click the login button",
    "* bullet point item",
    # sentences (terminal punctuation)
    "注：携帯メールアドレスには送信されない。",
    "This section describes the system architecture.",
    "本系统支持多用户并发访问,且具备完整的权限管理。",
    "・ デイリーメールが送信される時間から 24 時間以内に登録されている。",
]


def test_real_headings_kept():
    c = _chunker()
    for t in REAL_HEADINGS:
        assert c._looks_like_real_heading(t), f"real heading wrongly dropped: {t!r}"


def test_pseudo_headings_rejected():
    c = _chunker()
    for t in PSEUDO_HEADINGS:
        assert not c._looks_like_real_heading(t), f"pseudo heading wrongly kept: {t!r}"


def test_filter_disabled_keeps_everything():
    c = _chunker(enabled=False)
    for t in REAL_HEADINGS + PSEUDO_HEADINGS:
        assert c._looks_like_real_heading(t), f"disabled filter dropped: {t!r}"


def test_dotted_decimal_heading_with_text_survives():
    # A numbered heading WITH text must survive — dotted decimals are the most
    # common real section numbering, and the list-prefix regex must NOT match
    # them (it only matches "N)" / bullets, not "N.").
    c = _chunker()
    for t in ["2.1. 権限", "3.5.1. 属性作成・変更", "10.4.2 Methodology",
              "1. Background"]:
        assert c._looks_like_real_heading(t), f"numbered heading dropped: {t!r}"


def test_bare_number_only_is_dropped():
    # A heading that is JUST a number with no title text ("2.", "10.4.2.") ends
    # in "." → treated as non-heading. This is correct: a bare "2." makes a
    # meaningless title_path segment, so demoting it loses nothing. Documented
    # here so the behaviour is intentional, not an accident.
    c = _chunker()
    for t in ["2.", "10.4.2."]:
        assert not c._looks_like_real_heading(t), f"bare number kept: {t!r}"


def test_empty_title_rejected():
    c = _chunker()
    assert not c._looks_like_real_heading("")
    assert not c._looks_like_real_heading("   ")


# ---- end-to-end through chunk(): pseudo heading must not enter title_path ----

def test_pseudo_heading_demoted_to_body_in_chunks():
    md = (
        "# Real Section\n\n"
        + "Body text for the real section. " * 30 + "\n\n"
        "## ・ this is a mislabeled list item\n\n"
        + "More body content that belongs to the real section. " * 30 + "\n"
    )
    c = _chunker()
    chunks = c.chunk(md, {"format": "pdf"})
    # The bullet line must NOT appear as a title_path segment anywhere.
    for ch in chunks:
        tp = ch.metadata.get("title_path", "")
        assert "mislabeled list item" not in tp, (
            f"pseudo heading leaked into title_path: {tp!r}"
        )
    # Its text content must still be present somewhere (not dropped).
    joined = "\n".join(ch.text for ch in chunks)
    assert "mislabeled list item" in joined, "pseudo heading content was lost"


def test_real_heading_still_creates_title_path():
    md = (
        "# Chapter One\n\n"
        + "Content of chapter one goes here. " * 30 + "\n\n"
        "## 1.1 Real Subsection\n\n"
        + "Subsection content here, long enough to matter. " * 30 + "\n"
    )
    c = _chunker()
    chunks = c.chunk(md, {"format": "pdf"})
    paths = [ch.metadata.get("title_path", "") for ch in chunks]
    assert any("Chapter One" in p for p in paths), f"real heading lost: {paths}"
    assert any("Real Subsection" in p for p in paths), f"real subheading lost: {paths}"


def test_disabled_filter_lets_pseudo_through():
    # With the filter off, the bullet IS treated as a heading (legacy behaviour).
    md = (
        "# Real Section\n\n" + "Body. " * 30 + "\n\n"
        "## ・ bogus list item heading\n\n" + "More. " * 30 + "\n"
    )
    c = _chunker(enabled=False)
    chunks = c.chunk(md, {"format": "pdf"})
    paths = " ".join(ch.metadata.get("title_path", "") for ch in chunks)
    assert "bogus list item heading" in paths, (
        "disabled filter should let the pseudo heading into title_path"
    )
