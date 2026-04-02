"""
Chunks writer — outputs chunks.jsonl (one JSON object per line).

JSONL format is chosen for:
  - Streaming-friendly: can be read line by line without loading entire file
  - Appendable: easy to add chunks incrementally
  - Compatible with most RAG/embedding pipelines

Each line follows the schema defined in DESIGN.md §6.10.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..chunkers.base import Chunk
from ..config import get_nested


def write_chunks(
    chunks: list[Chunk],
    output_dir: Path,
    config: dict[str, Any],
    append: bool = False,
) -> Path:
    """
    Write chunks to chunks.jsonl.

    Args:
        chunks: List of Chunk objects to write.
        output_dir: Base output directory (e.g., ./knowledge/).
        config: Full config dict.
        append: If True, append to existing file. If False, overwrite.

    Returns:
        Path to the written chunks.jsonl file.
    """
    filename = get_nested(config, "chunking.output_file", "chunks.jsonl")
    chunks_path = output_dir / filename

    mode = "a" if append else "w"
    with open(chunks_path, mode, encoding="utf-8") as f:
        for chunk in chunks:
            record = {
                "id": _build_chunk_id(chunk),
                "text": chunk.text,
                "metadata": chunk.metadata,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return chunks_path


def _build_chunk_id(chunk: Chunk) -> str:
    """
    Build a deterministic chunk ID.

    Format: {source_stem}_chunk_{index:03d}
    Example: annual-report-2025_chunk_012
    """
    source = chunk.metadata.get("original_file", chunk.metadata.get("source", "unknown"))
    # Extract stem from filename
    stem = Path(source).stem if source else "unknown"
    # Sanitize: replace spaces and special chars
    stem = stem.replace(" ", "-").replace("/", "-").replace("\\", "-")
    index = chunk.metadata.get("chunk_index", 0)
    return f"{stem}_chunk_{index:03d}"
