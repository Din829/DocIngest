"""
Chunk-level extraction cache for the graph layer.

Why a separate cache from ``docingest.incremental`` and from LightRAG's own
LLM cache:

- ``docingest.incremental`` is keyed on ORIGINAL FILE content + main-pipeline
  config. It tells us "this PDF didn't change, reuse the produced chunks".
  It says nothing about whether entity extraction was already done on a
  particular chunk.

- LightRAG's ``enable_llm_cache`` is keyed on EXACT PROMPT + MODEL. It
  catches duplicate prompts within a single run, but doesn't help us
  decide "should we even feed chunk X into LightRAG at all this time?".

This cache fills the gap: a tiny file recording, per chunk_id, the
``(content_hash, llm_config_hash)`` we last extracted with. On the next
build we skip chunks whose pair matches — no ainsert call, no LightRAG
work, no LLM cost.

All three layers compose:
    main-pipeline incremental → chunk unchanged ⇒ chunk reused as-is
    graph cache                → extraction inputs unchanged ⇒ skip ainsert
    LightRAG LLM cache         → identical prompts within a build ⇒ skip LLM call

Storage layout (under ``output_dir / graph.cache.cache_dir``):
    extraction_state.json    — single JSON file mapping chunk_id → entry.
                               Atomic full-file rewrites; for the volumes
                               the graph layer handles (a few thousand
                               chunks at most), this beats per-chunk files
                               in I/O cost and is trivially crash-safe via
                               write-then-rename.

Schema (version 1):
    {
        "version": 1,
        "entries": {
            "<chunk_id>": {
                "content_hash": "<md5 of chunk text>",
                "llm_config_hash": "<short hash of relevant graph LLM cfg>",
                "extracted_at": "<ISO timestamp>"
            },
            ...
        }
    }
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_nested


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

def chunk_content_hash(text: str) -> str:
    """
    MD5 of the chunk text. Cheap and stable — only used for change detection,
    not security. Keep this in sync with anything else hashing chunk content
    so the two never disagree about whether a chunk "changed".
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# Subset of config that, when changed, must invalidate prior extractions.
# Anything that can change WHAT gets extracted from a chunk goes here;
# anything that only affects WHERE the output is stored stays out (mirrors
# the design of incremental._RELEVANT_CONFIG_PATHS).
_RELEVANT_LLM_PATHS = [
    "graph.llm.primary.provider",
    "graph.llm.primary.model",
    "graph.llm.fallback.provider",
    "graph.llm.fallback.model",
    "graph.llm.max_response_tokens",
    "graph.lightrag.entity_extract_max_gleaning",
    # NOTE: chunk_token_size NOT included — it changes how LightRAG splits
    # input internally, but our cache is keyed by chunk_id (the DocIngest
    # chunk id), so internal re-chunking is invisible at this layer.
]


def llm_config_hash(config: dict[str, Any]) -> str:
    """
    Stable short hash of the graph LLM config knobs that affect extraction.

    Returns 16 hex chars — same length as ``incremental.compute_config_hash``
    so debug output stays uniform across DocIngest's caches.
    """
    subset: dict[str, Any] = {}
    for path in _RELEVANT_LLM_PATHS:
        subset[path] = get_nested(config, path)
    serialized = json.dumps(subset, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1
_STATE_FILENAME = "extraction_state.json"


def _state_path(cache_dir: Path) -> Path:
    return cache_dir / _STATE_FILENAME


def load_state(cache_dir: Path) -> dict[str, dict[str, Any]]:
    """
    Load the extraction state. Returns ``{}`` on any read failure or version
    mismatch — callers treat an empty mapping as "nothing cached yet" and
    re-extract everything, which is the safe behaviour.
    """
    path = _state_path(cache_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_state(cache_dir: Path, entries: dict[str, dict[str, Any]]) -> None:
    """
    Atomic full-file write: serialise to a sibling .tmp then rename. The
    rename is atomic on POSIX and on Windows (since Python 3.3 os.replace).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    final = _state_path(cache_dir)
    tmp = final.with_suffix(final.suffix + ".tmp")
    payload = {"version": _SCHEMA_VERSION, "entries": entries}
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, final)


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------

def is_chunk_cached(
    entries: dict[str, dict[str, Any]],
    chunk_id: str,
    content_hash: str,
    llm_hash: str,
) -> bool:
    """
    Return True iff there's a prior entry for ``chunk_id`` whose content
    AND llm config still match. Either mismatch → re-extract.
    """
    entry = entries.get(chunk_id)
    if entry is None:
        return False
    return (
        entry.get("content_hash") == content_hash
        and entry.get("llm_config_hash") == llm_hash
    )


def mark_extracted(
    entries: dict[str, dict[str, Any]],
    chunk_id: str,
    content_hash: str,
    llm_hash: str,
) -> None:
    """In-place update; caller persists by calling ``save_state``."""
    entries[chunk_id] = {
        "content_hash": content_hash,
        "llm_config_hash": llm_hash,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
    }


__all__ = [
    "chunk_content_hash",
    "llm_config_hash",
    "load_state",
    "save_state",
    "is_chunk_cached",
    "mark_extracted",
]
