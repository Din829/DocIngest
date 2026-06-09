"""Regression tests for slide boundary detection."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.api import build_config
from docingest.chunkers.slide import SlideChunker


def _chunker() -> SlideChunker:
    return SlideChunker(build_config())


def test_numbered_headings_split_without_label_keywords() -> None:
    markdown = (
        "# 幻灯片1\n\n第一页内容\n\n"
        "# 第2页\n\n第二页内容\n\n"
        "# Folie 3\n\n第三页内容\n"
    )

    chunks = _chunker().chunk(markdown, {"source": "deck.md", "format": "pptx"})

    assert len(chunks) == 3
    assert chunks[0].metadata["title_path"] == "幻灯片1"
    assert chunks[1].metadata["title_path"] == "第2页"
    assert chunks[2].metadata["title_path"] == "Folie 3"


def test_plain_number_markers_split() -> None:
    markdown = (
        "Intro before slides\n\n"
        "# 1\n\nFirst slide\n\n"
        "# 2\n\nSecond slide\n"
    )

    chunks = _chunker().chunk(markdown, {"source": "deck.md", "format": "pptx"})

    assert len(chunks) == 3
    assert chunks[0].text == "Intro before slides"
    assert chunks[1].metadata["title_path"] == "1"
    assert chunks[2].metadata["title_path"] == "2"


def test_descriptive_chapter_headings_do_not_trigger_slide_fallback() -> None:
    markdown = (
        "# Chapter 1\n\n"
        + ("This is ordinary document content. " * 80)
        + "\n\n# Chapter 2\n\n"
        + ("More ordinary document content. " * 80)
    )

    chunks = _chunker().chunk(markdown, {"source": "book.md", "format": "pptx"})

    assert chunks
    assert all("slide_index" not in chunk.metadata for chunk in chunks)
    assert all("title_path" not in chunk.metadata for chunk in chunks)


def test_numbered_headings_must_be_sequential() -> None:
    markdown = (
        "# 1\n\nFirst slide\n\n"
        "# 3\n\nThird slide\n"
    )

    chunks = _chunker().chunk(markdown, {"source": "deck.md", "format": "pptx"})

    assert len(chunks) == 1
    assert "First slide" in chunks[0].text
    assert "Third slide" in chunks[0].text


def main() -> None:
    test_numbered_headings_split_without_label_keywords()
    test_plain_number_markers_split()
    test_descriptive_chapter_headings_do_not_trigger_slide_fallback()
    test_numbered_headings_must_be_sequential()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
