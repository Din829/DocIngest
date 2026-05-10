"""
chunks.jsonl → LightRAG-ready document records.

Responsibilities:
    1. Read chunks.jsonl line-by-line (streaming — chunks files can be 100K+
       lines on large knowledge bases, loading via json.load() would burn
       memory).
    2. Apply config-driven filters (formats whitelist, min_chunk_tokens).
    3. Enrich each chunk's text with its DocIngest title_path so the LLM
       extractor sees the structural context the main pipeline already
       computed. This costs ~10-30 tokens per chunk and measurably improves
       entity disambiguation across documents (cf. ARCHITECTURE.md §5.6 on
       path injection).
    4. Yield ``LoadedChunk`` records the backend can pass straight into
       LightRAG's ``ainsert(texts=..., ids=..., file_paths=...)`` call.

We deliberately do NOT mutate the chunk metadata or write anything back
into chunks.jsonl — the file is shared with RAG / agentic-search consumers
and must stay byte-stable across graph builds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass
class LoadedChunk:
    """
    One chunk after filtering + title_path enrichment, ready to feed to
    a graph backend.

    Fields:
        chunk_id        — DocIngest's deterministic chunk id (used as
                          LightRAG document id and as the cache key).
        text            — Enriched text: title_path prepended to chunk body.
                          Backends pass this verbatim to ``ainsert``.
        original_text   — Raw chunk text, unmodified. Used for content
                          hashing in the cache (so re-running with a
                          different title_path doesn't spuriously
                          invalidate cached extractions).
        source_file     — sources/<name>.md path (relative to output_dir
                          where possible). Surfaced as LightRAG's
                          ``file_paths`` argument so the backend can later
                          attribute entities back to source docs.
        metadata        — A shallow copy of the chunk's metadata, untouched.
                          Backends use it for downstream attribution but do
                          not feed it back into LLM prompts (LightRAG's
                          schema doesn't accept arbitrary metadata).
    """

    chunk_id: str
    text: str
    original_text: str
    source_file: str
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Token estimation (matches chunkers/base.py philosophy: CJK ≈ 1.5 tok,
# ASCII ≈ 0.25 tok per char). We need this only for the min_chunk_tokens
# filter so chunks.jsonl noise (page numbers, single-row footers) doesn't
# burn LLM budget on entity extraction.
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(
        1 for c in text
        if (
            "一" <= c <= "鿿"        # CJK Unified Ideographs
            or "぀" <= c <= "ヿ"     # Hiragana + Katakana
            or "가" <= c <= "힯"     # Hangul
        )
    )
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.25)


# ---------------------------------------------------------------------------
# Title-path enrichment
# ---------------------------------------------------------------------------

def _enrich_with_title_path(text: str, metadata: dict[str, Any]) -> str:
    """
    Prepend the document/section path to the chunk text as a small markdown
    header so the LLM sees structural context.

    Coordination with the main pipeline:
        DocIngest's main pipeline has its OWN path_injection hook
        (``chunking.enrichment.path_injection``, default true) that
        already prepends a ``[来源: sources/xxx.md > section]`` line into
        every chunk it writes to chunks.jsonl. When that's the case we
        MUST NOT add a second header — the LLM extractor would just see
        two slightly-different paths and waste tokens.

    Detection rule:
        If the text already starts with ``[来源:`` (the canonical marker
        the main pipeline writes) OR with a ``[<basename> > ...]`` block
        identical to the one we'd write, we leave the text alone.

    When NO path header is present (path_injection disabled, or chunks
    produced by an older pipeline version), we prepend a fresh one so
    GraphRAG quality doesn't degrade.
    """
    # If the main pipeline already injected its path header, leave it
    # alone — second header is pure noise. The marker is the literal
    # "[来源:" prefix path_injector.py emits (see enrichment/path_injector.py).
    stripped = text.lstrip()
    if stripped.startswith("[来源:") or stripped.startswith("[Source:"):
        return text

    parts: list[str] = []

    # Prefer the original_file basename — clearer than the markdown source
    # path for users reading entity attributions.
    src = metadata.get("original_file") or metadata.get("source") or ""
    if src:
        # Strip directory components for readability; full path is still
        # available via metadata.original_file in the cache.
        name = src.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        parts.append(name)

    title_path = metadata.get("title_path") or ""
    if title_path:
        parts.append(str(title_path))

    if not parts:
        return text

    header = "[" + " > ".join(parts) + "]\n"
    if text.startswith(header):
        return text
    return header + text


# ---------------------------------------------------------------------------
# Streaming reader + filter
# ---------------------------------------------------------------------------

def iter_chunks(
    chunks_path: Path,
    *,
    formats: list[str] | None = None,
    min_chunk_tokens: int = 0,
) -> Iterator[LoadedChunk]:
    """
    Stream chunks.jsonl, yielding chunks that pass the filters.

    Malformed lines are skipped silently (mirrors how the main pipeline's
    ``incremental.load_chunks_by_id`` handles them) — a single bad line
    must not abort a 50K-chunk graph build.

    Args:
        chunks_path: Path to chunks.jsonl. Must exist; caller is responsible
            for "build before chunks exist" handling.
        formats: Lowercase format whitelist (e.g. ["pdf", "docx"]). None /
            empty list = pass through every chunk regardless of format.
        min_chunk_tokens: Drop chunks whose token estimate is below this.
            0 = no filter.

    Yields:
        LoadedChunk records in file order. Order is preserved so backends
        get deterministic ainsert behaviour.
    """
    formats_set = {f.lower() for f in formats} if formats else None

    with open(chunks_path, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            chunk_id = record.get("id")
            text = record.get("text")
            metadata = record.get("metadata") or {}
            if not chunk_id or not isinstance(text, str) or not text.strip():
                continue

            # format filter
            fmt = str(metadata.get("format", "")).lower()
            if formats_set is not None and fmt not in formats_set:
                continue

            # min token filter (uses raw chunk text, not enriched — title_path
            # tokens shouldn't bias the noise filter against well-structured
            # short chunks).
            if min_chunk_tokens > 0 and _estimate_tokens(text) < min_chunk_tokens:
                continue

            source_file = str(metadata.get("source") or metadata.get("original_file") or "")
            enriched = _enrich_with_title_path(text, metadata)

            yield LoadedChunk(
                chunk_id=str(chunk_id),
                text=enriched,
                original_text=text,
                source_file=source_file,
                metadata=metadata,
            )


def load_all(
    chunks_path: Path,
    *,
    formats: list[str] | None = None,
    min_chunk_tokens: int = 0,
) -> list[LoadedChunk]:
    """
    Materialise all matching chunks into a list. Convenience for callers
    that need the count up-front (e.g. progress bars). Uses ``iter_chunks``
    internally so filtering rules stay in one place.
    """
    return list(
        iter_chunks(
            chunks_path,
            formats=formats,
            min_chunk_tokens=min_chunk_tokens,
        )
    )


__all__ = [
    "LoadedChunk",
    "iter_chunks",
    "load_all",
]
