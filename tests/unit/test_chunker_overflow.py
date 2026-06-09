"""Regression tests for protected-block overflow strategies."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.api import build_config
from docingest.chunkers.recursive import RecursiveChunker


def _chunker(**overrides) -> RecursiveChunker:
    config = build_config(config_overrides=overrides or None)
    return RecursiveChunker(config)


def test_long_list_splits_by_items() -> None:
    ch = _chunker()
    markdown = "\n".join(
        f"- Item {i}: " + ("detail " * 260)
        for i in range(8)
    )

    chunks = ch.chunk(markdown, {"source": "list.md", "format": "md"})
    tokens = [c.metadata["tokens"] for c in chunks]

    assert len(chunks) > 1, tokens
    assert max(tokens) <= ch._max_tokens * ch._get_overflow("list"), tokens
    assert sum(c.text.count("- Item") for c in chunks) == 8


def test_single_oversized_list_item_still_splits() -> None:
    ch = _chunker()
    markdown = "- One huge item: " + ("これは長い説明です。" * 900)

    chunks = ch.chunk(markdown, {"source": "list.md", "format": "md"})
    tokens = [c.metadata["tokens"] for c in chunks]

    assert len(chunks) > 1, tokens
    assert max(tokens) <= ch._max_tokens * ch._get_overflow("default"), tokens
    assert "".join(c.text for c in chunks).replace("\n\n", "").startswith("- One huge item:")


def test_nested_list_items_stay_with_parent() -> None:
    items = RecursiveChunker._list_items(
        "- Parent A\n"
        "  - Child A1\n"
        "  - Child A2\n"
        "- Parent B\n"
        "  continuation\n"
    )

    assert len(items) == 2, items
    assert "Child A1" in items[0]
    assert items[1].startswith("- Parent B")


def test_table_row_split_still_works() -> None:
    ch = _chunker()
    rows = ["| col1 | col2 |", "| --- | --- |"]
    rows += [f"| row{i} | " + ("長い内容" * 120) + " |" for i in range(20)]
    markdown = "\n".join(rows)

    chunks = ch.chunk(markdown, {"source": "table.md", "format": "md"})
    tokens = [c.metadata["tokens"] for c in chunks]

    assert len(chunks) > 1, tokens
    assert all("| col1 | col2 |" in chunk.text for chunk in chunks)


def main() -> None:
    test_long_list_splits_by_items()
    test_single_oversized_list_item_still_splits()
    test_nested_list_items_stay_with_parent()
    test_table_row_split_still_works()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
