"""
Incremental processing — content-addressed cache for pipeline outputs.

Design:
  - Cache key is PURE content hash (head + tail bytes + file size), not path.
  - Same file moved/renamed → still hits cache.
  - Config changes affecting output → auto-invalidates via config_hash.
  - Missing output files (sources/, assets/) → auto-invalidates.

Cache structure:
  {output_dir}/.cache/{cache_key}.meta.json

Meta schema:
  {
    "version": 1,
    "cache_key": "<md5head_size>",
    "config_hash": "<short hash of relevant config>",
    "original_name": "spec.xlsx",
    "last_seen_path": "/absolute/path/to/spec.xlsx",
    "processed_at": "ISO timestamp",
    "format": "xlsx",
    "outputs": {
        "source_md": "sources/spec.md",
        "assets": ["assets/spec-image1.png", ...],
        "chunk_ids": ["spec_chunk_000", ...]
    },
    "index_entry": { ... full index.json entry for this file ... }
  }
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_nested


# ---------------------------------------------------------------------------
# Content-addressed cache key
# ---------------------------------------------------------------------------

def compute_cache_key(file_path: Path, sample_size: int = 8192) -> str:
    """
    Compute a cache key for a file.

    Key = MD5(head_bytes + tail_bytes + size + original_filename)

    Why include filename:
      - Two files with identical content but different names should produce
        separate output entries (sources/a.md and sources/b.md), each with
        their own index entry and chunks. If we keyed only on content, they
        would collide and overwrite each other's cache.
      - Consequence: renaming a file triggers a re-run (one-time cost).
        This is an acceptable tradeoff for correctness.

    Why head+tail sampling:
      - Fast: O(1) disk reads regardless of file size
      - Collision-resistant when combined with size

    Key is path-directory-independent: same file in different directories
    still hits the same cache (we only use the filename, not the full path).

    Args:
        file_path: Path to the file.
        sample_size: Bytes to read from head and tail (default 8192).

    Returns:
        Cache key string like "a3f8e9b2c1d4e5f6_127320".

    Raises:
        FileNotFoundError, PermissionError if file can't be read.
    """
    size = file_path.stat().st_size

    if size <= sample_size * 2:
        # Small file: hash entire content
        content = file_path.read_bytes()
    else:
        # Large file: sample head + tail
        with open(file_path, "rb") as f:
            head = f.read(sample_size)
            f.seek(-sample_size, os.SEEK_END)
            tail = f.read(sample_size)
        content = head + tail

    # Mix in the filename to distinguish same-content files with different names
    hasher = hashlib.md5()
    hasher.update(content)
    hasher.update(b"|")
    hasher.update(file_path.name.encode("utf-8"))
    digest = hasher.hexdigest()
    return f"{digest}_{size}"


# ---------------------------------------------------------------------------
# Config hash (identifies config changes that affect output)
# ---------------------------------------------------------------------------

# Only config paths that actually affect the output content.
# Changing output.dir or performance.parallel_files does NOT invalidate cache.
_RELEVANT_CONFIG_PATHS = [
    "parsing.engine",
    "parsing.ocr.engine",
    "parsing.ocr.languages",
    "parsing.ocr.force",
    "parsing.vision.enabled",
    "parsing.vision.image_dpi",
    "parsing.docx.vision_page_images",
    "parsing.docx.max_page_images",
    "parsing.docx.max_image_pixels",
    "parsing.pptx.vision_page_images",
    "parsing.pptx.max_page_images",
    "parsing.pptx.max_image_pixels",
    "parsing.pdf.table_extraction",
    "parsing.pdf.image_extraction",
    "parsing.pptx.include_notes",
    "parsing.xlsx.row_to_text",
    "parsing.xlsx.max_rows",
    "parsing.xlsx.include_formulas",
    "parsing.xlsx.denoising",
    "chunking.enabled",
    "chunking.strategy",
    "chunking.max_tokens",
    "chunking.min_tokens",
    "chunking.overlap_tokens",
    "chunking.heading",
    "chunking.auto",
    "chunking.protection",
    "chunking.enrichment.path_injection",
    "models.vision.primary.provider",
    "models.vision.primary.model",
    "models.vision.fallback.provider",
    "models.vision.fallback.model",
    "output.markdown.include_metadata_header",
]


def compute_config_hash(config: dict[str, Any]) -> str:
    """
    Hash the subset of config that affects pipeline output.

    Returns a short hex string (16 chars). Changes to unrelated config
    (output.dir, knowledge_map.*, refine.*) do not change this hash.
    """
    subset: dict[str, Any] = {}
    for path in _RELEVANT_CONFIG_PATHS:
        subset[path] = get_nested(config, path)

    serialized = json.dumps(subset, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Meta file I/O
# ---------------------------------------------------------------------------

def get_meta_path(cache_dir: Path, cache_key: str) -> Path:
    """Resolve the meta.json path for a given cache key."""
    return cache_dir / f"{cache_key}.meta.json"


def load_cached_meta(cache_dir: Path, cache_key: str) -> dict[str, Any] | None:
    """
    Load cached metadata for a given cache key.

    Returns None if the meta file doesn't exist or is corrupt.
    """
    meta_path = get_meta_path(cache_dir, cache_key)
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_cached_meta(cache_dir: Path, meta: dict[str, Any]) -> None:
    """Persist metadata to disk. Creates cache_dir if needed."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta_path = get_meta_path(cache_dir, meta["cache_key"])
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Cache validity check
# ---------------------------------------------------------------------------

def is_cache_valid(
    meta: dict[str, Any],
    current_config_hash: str,
    output_dir: Path,
    old_chunks_by_id: dict[str, Any],
) -> tuple[bool, str]:
    """
    Check whether a cached meta entry can be reused for the current run.

    Checks:
      1. Schema version matches
      2. config_hash matches current config
      3. Output Markdown file still exists on disk
      4. All referenced asset files still exist
      5. All chunk_ids can be found in the current chunks.jsonl

    Args:
        meta: The cached meta dict (from load_cached_meta).
        current_config_hash: The current run's config hash.
        output_dir: Base output directory.
        old_chunks_by_id: Dict mapping chunk_id → chunk record from old chunks.jsonl.

    Returns:
        (is_valid, reason). reason is empty string if valid, otherwise explains why not.
    """
    if meta.get("version") != 1:
        return False, f"meta schema version mismatch (got {meta.get('version')})"

    if meta.get("config_hash") != current_config_hash:
        return False, "config changed"

    outputs = meta.get("outputs", {})

    # Check source markdown exists
    source_md_rel = outputs.get("source_md", "")
    if not source_md_rel:
        return False, "meta missing source_md"
    source_md_path = output_dir / source_md_rel
    if not source_md_path.exists():
        return False, f"source markdown missing: {source_md_rel}"

    # Check assets exist
    for asset_rel in outputs.get("assets", []):
        asset_path = output_dir / asset_rel
        if not asset_path.exists():
            return False, f"asset missing: {asset_rel}"

    # Check chunk_ids are in old chunks.jsonl
    chunk_ids = outputs.get("chunk_ids", [])
    if chunk_ids:
        missing_chunks = [cid for cid in chunk_ids if cid not in old_chunks_by_id]
        if missing_chunks:
            return False, f"chunks missing from chunks.jsonl: {missing_chunks[:3]}"

    return True, ""


# ---------------------------------------------------------------------------
# chunks.jsonl index loader
# ---------------------------------------------------------------------------

def load_chunks_by_id(chunks_path: Path) -> dict[str, dict[str, Any]]:
    """
    Load chunks.jsonl into a {chunk_id: chunk_record} dict for fast lookup.

    Returns empty dict if file doesn't exist or is unreadable.
    """
    if not chunks_path.exists():
        return {}

    result: dict[str, dict[str, Any]] = {}
    try:
        with open(chunks_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    chunk_id = record.get("id")
                    if chunk_id:
                        result[chunk_id] = record
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return result


# ---------------------------------------------------------------------------
# Meta construction from pipeline result
# ---------------------------------------------------------------------------

def build_meta(
    file_path: Path,
    cache_key: str,
    config_hash: str,
    format_str: str,
    source_md_rel: str,
    asset_rels: list[str],
    chunk_ids: list[str],
    index_entry: dict[str, Any],
) -> dict[str, Any]:
    """
    Build a meta dict ready for save_cached_meta.

    All path fields use forward slashes for cross-platform consistency.
    """
    return {
        "version": 1,
        "cache_key": cache_key,
        "config_hash": config_hash,
        "original_name": file_path.name,
        "last_seen_path": str(file_path.resolve()).replace("\\", "/"),
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "format": format_str,
        "outputs": {
            "source_md": source_md_rel.replace("\\", "/"),
            "assets": [a.replace("\\", "/") for a in asset_rels],
            "chunk_ids": list(chunk_ids),
        },
        "index_entry": index_entry,
    }
