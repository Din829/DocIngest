"""
Chunks writer — outputs chunks.jsonl (one JSON object per line).

JSONL format is chosen for:
  - Streaming-friendly: can be read line by line without loading entire file
  - Appendable: easy to add chunks incrementally
  - Compatible with most RAG/embedding pipelines

Each line follows the chunk schema documented in ARCHITECTURE.md (see §5.6 Chunking).
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
    records = [
        {
            "id": build_chunk_id(chunk),
            "text": chunk.text,
            "metadata": chunk.metadata,
        }
        for chunk in chunks
    ]
    return write_chunk_records(records, output_dir, config, append=append)


def write_chunk_records(
    records: list[dict[str, Any]],
    output_dir: Path,
    config: dict[str, Any],
    append: bool = False,
) -> Path:
    """
    Write pre-built chunk records (dicts with id/text/metadata) to chunks.jsonl.

    Used by incremental mode to merge reused chunks (from cache) with new chunks
    in a single write. Records are expected to have 'id', 'text', 'metadata' keys.

    Args:
        records: List of chunk record dicts ready for serialization.
        output_dir: Base output directory.
        config: Full config dict.
        append: If True, append to existing file. If False, overwrite.

    Returns:
        Path to the written chunks.jsonl file.
    """
    filename = get_nested(config, "chunking.output_file", "chunks.jsonl")
    chunks_path = output_dir / filename

    mode = "a" if append else "w"
    with open(chunks_path, mode, encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return chunks_path


def build_chunk_id(chunk: Chunk) -> str:
    """
    Build a deterministic chunk ID.

    Format: {source_stem}_chunk_{index:03d}
    Example: annual-report-2025_chunk_012

    Public (unprefixed) so incremental cache can use the same scheme.
    """
    source = chunk.metadata.get("original_file", chunk.metadata.get("source", "unknown"))
    # Extract stem from filename
    stem = Path(source).stem if source else "unknown"
    # Sanitize: replace spaces and special chars
    stem = stem.replace(" ", "-").replace("/", "-").replace("\\", "-")
    index = chunk.metadata.get("chunk_index", 0)
    return f"{stem}_chunk_{index:03d}"


# Backward-compat alias (was private, now public)
_build_chunk_id = build_chunk_id
