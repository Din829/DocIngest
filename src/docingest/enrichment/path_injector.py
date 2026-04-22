"""
Path injector — prepends source path + title path to each chunk.

This is the "must-do, zero-cost" enrichment (see ARCHITECTURE.md §5.6 Chunking).
Makes every chunk self-contained: even without surrounding context,
a reader knows where this chunk came from.

Example:
  Before: "営業利益は前年比 15% 増加..."
  After:  "[来源: sources/report.md > 第4章 財務データ > 営業利益]\n営業利益は前年比 15% 増加..."
"""

from __future__ import annotations

from ..chunkers.base import Chunk


def inject_paths(chunks: list[Chunk]) -> list[Chunk]:
    """
    Prepend source path + title path to each chunk's text.

    Reads from chunk.metadata:
      - "source": file path (e.g., "sources/report.md")
      - "title_path": heading path (e.g., "第4章 > 営業利益")

    Modifies chunks in-place and returns the same list.
    """
    for chunk in chunks:
        source = chunk.metadata.get("source", "")
        title_path = chunk.metadata.get("title_path", "")

        # Build path prefix
        if source and title_path:
            prefix = f"[来源: {source} > {title_path}]"
        elif source:
            prefix = f"[来源: {source}]"
        else:
            continue  # No source info → skip injection

        # Prepend to text (only if not already injected)
        if not chunk.text.startswith("[来源:"):
            chunk.text = f"{prefix}\n{chunk.text}"

    return chunks
