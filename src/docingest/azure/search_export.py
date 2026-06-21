"""
Export DocIngest chunks → Azure AI Search (manual vectorization, generic).

This is the "manual vectorization" path Azure documents: you own chunking
(DocIngest already did it) and embedding (injected here), and push prevectorized
documents into a vector field. Azure AI Search does NOT host the embedding model.

Generic on purpose — nothing here assumes a particular index schema:
  - Field names come from ``field_map`` (defaults below; override per index).
  - Embedding comes from an injected provider (any object with .embed()/.dimension).
  - The vector field's dimension is verified against the embedding before any
    upload — Azure rejects mismatched dims, so we fail loud early with a clear
    message instead of a cryptic per-document SDK error mid-batch.

It reads chunks.jsonl itself (one cheap json.loads per line) rather than reusing
the langchain / graph loaders, so this plugin stays independent of those.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .embedding import SearchEmbeddingProvider

logger = logging.getLogger(__name__)


# Default chunk→index field mapping. Keys are DocIngest's logical names; values
# are the index field names. Override any value to match an existing index.
#
# Three keys are CORE and always written: id / content / vector. Every OTHER key
# in the map is treated as a metadata field — its value (the index field name)
# receives chunk.metadata[<key>] when that key is present on the chunk. So to
# push an extra metadata field that your index has (e.g. language, author), just
# add it to field_map — no code change:
#     field_map={"language": "lang", "author": "doc_author"}
# The defaults below cover the metadata DocIngest always produces; they are not
# a fixed allow-list.
DEFAULT_FIELD_MAP: dict[str, str] = {
    "id": "id",                    # chunk id   → document key (Edm.String, key=true)
    "content": "content",          # chunk text → human-readable field
    "vector": "content_vector",    # embedding  → Collection(Edm.Single) vector field
    "source": "source",            # metadata (copied from chunk.metadata if present)
    "title_path": "title_path",
    "format": "format",
}

# The three core keys handled explicitly in _build_doc; everything else in
# field_map is a metadata mapping. Defined here (not hardcoded per-field) so the
# metadata channel is open-ended — any chunk.metadata key the caller maps gets
# pushed.
_CORE_KEYS = ("id", "content", "vector")


@dataclass
class ExportResult:
    """Outcome of a push — caller-owned error handling (no raising on doc failures)."""
    total_chunks: int = 0
    uploaded: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


def _iter_chunks(chunks_path: Path):
    """Stream chunks.jsonl, yielding (id, text, metadata). Bad lines skipped
    (same policy as DocIngest's other chunk readers — one malformed line must
    not abort a large export)."""
    with open(chunks_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            cid = rec.get("id")
            text = rec.get("text")
            meta = rec.get("metadata") or {}
            if not cid or not isinstance(text, str) or not text.strip():
                continue
            yield str(cid), text, meta


def _build_doc(
    cid: str, text: str, meta: dict[str, Any], vector: list[float], fmap: dict[str, str]
) -> dict[str, Any]:
    """Map one chunk + its vector into an Azure Search document dict."""
    doc: dict[str, Any] = {
        fmap["id"]: cid,
        fmap["content"]: text,
        fmap["vector"]: vector,
    }
    # Every non-core key in field_map is a metadata mapping: logical key →
    # index field name. Push it when the chunk actually carries that metadata.
    # Open-ended by design — map any chunk.metadata key your index has.
    for logical_key, index_field in fmap.items():
        if logical_key in _CORE_KEYS:
            continue
        value = meta.get(logical_key)
        if value is not None:
            doc[index_field] = value
    return doc


def _resolve_chunks_path(kb_dir: str | Path, chunks_filename: str) -> Path:
    p = Path(kb_dir)
    path = p / chunks_filename if p.is_dir() else p
    if not path.exists():
        raise FileNotFoundError(f"chunks.jsonl not found: {path}")
    return path


def push_to_azure_search(
    kb_dir: str | Path,
    *,
    search_endpoint: str,
    index_name: str,
    embedding: SearchEmbeddingProvider,
    search_key: str | None = None,
    field_map: dict[str, str] | None = None,
    batch_size: int = 100,
    chunks_filename: str = "chunks.jsonl",
) -> ExportResult:
    """Embed a knowledge base's chunks and upload them to an Azure AI Search index.

    Args:
        kb_dir: knowledge dir (containing chunks.jsonl) or a chunks.jsonl path.
        search_endpoint: https://<service>.search.windows.net
        index_name: target index (must already exist with a matching vector field;
            use create_azure_search_index() to make one).
        embedding: any object exposing .embed(list[str])->list[list[float]] and
            .dimension (see azure.embedding providers).
        search_key: admin key. If None, falls back to AZURE_SEARCH_API_KEY env,
            else DefaultAzureCredential (Entra ID).
        field_map: override DEFAULT_FIELD_MAP per index. Missing keys inherit
            the default.
        batch_size: chunks embedded + uploaded per round.

    Returns ExportResult (uploaded / failed / errors); does not raise on
    per-document upload failures — caller inspects the result.
    """
    fmap = {**DEFAULT_FIELD_MAP, **(field_map or {})}
    chunks_path = _resolve_chunks_path(kb_dir, chunks_filename)

    client = _make_search_client(search_endpoint, index_name, search_key)

    # System-edge check: verify the index's vector field dimension matches the
    # embedding BEFORE spending money embedding a whole corpus we can't upload.
    _verify_dimension(search_endpoint, index_name, search_key, fmap["vector"], embedding.dimension)

    result = ExportResult()
    batch: list[tuple[str, str, dict[str, Any]]] = []

    def _flush() -> None:
        if not batch:
            return
        texts = [t for _, t, _ in batch]
        vectors = embedding.embed(texts)
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"embedding returned {len(vectors)} vectors for {len(batch)} "
                f"texts — provider contract violated."
            )
        docs = [
            _build_doc(cid, text, meta, vec, fmap)
            for (cid, text, meta), vec in zip(batch, vectors)
        ]
        _upload(client, docs, result)
        batch.clear()

    for cid, text, meta in _iter_chunks(chunks_path):
        result.total_chunks += 1
        batch.append((cid, text, meta))
        if len(batch) >= batch_size:
            _flush()
    _flush()

    logger.info(
        "Azure Search export: %d/%d uploaded, %d failed",
        result.uploaded, result.total_chunks, result.failed,
    )
    return result


# --- Azure SDK plumbing (system edge; SDK imported lazily) -----------------

def _make_search_client(endpoint: str, index_name: str, key: str | None):
    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
    except ImportError as e:
        raise ImportError(
            "Azure Search export requires azure-search-documents. "
            "Install with: pip install -e \".[azure]\"."
        ) from e

    import os
    key = key or os.environ.get("AZURE_SEARCH_API_KEY")
    if key:
        return SearchClient(endpoint, index_name, AzureKeyCredential(key))
    # No key → Entra ID. Imported only on this path so key users don't need it.
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as e:
        raise ImportError(
            "No search_key / AZURE_SEARCH_API_KEY set and azure-identity is not "
            "installed for Entra ID auth. Either pass a key or "
            "pip install azure-identity."
        ) from e
    return SearchClient(endpoint, index_name, DefaultAzureCredential())


def _verify_dimension(
    endpoint: str, index_name: str, key: str | None, vector_field: str, expected_dim: int
) -> None:
    """Read the index schema and assert the vector field's dimension matches the
    embedding. Fail loud on mismatch — Azure would otherwise reject every upload
    with a confusing per-document error.

    If the schema can't be read (permissions / older SDK), we log and continue
    rather than block the export — the upload itself remains the real gate."""
    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents.indexes import SearchIndexClient
    except ImportError:
        return
    import os
    key = key or os.environ.get("AZURE_SEARCH_API_KEY")
    try:
        if key:
            idx_client = SearchIndexClient(endpoint, AzureKeyCredential(key))
        else:
            from azure.identity import DefaultAzureCredential
            idx_client = SearchIndexClient(endpoint, DefaultAzureCredential())
        index = idx_client.get_index(index_name)
    except Exception as e:
        logger.warning(
            "Could not read index '%s' schema to verify vector dimension "
            "(%s: %s); proceeding — the upload will surface any real mismatch.",
            index_name, type(e).__name__, e,
        )
        return

    for f in getattr(index, "fields", []) or []:
        if getattr(f, "name", None) == vector_field:
            actual = getattr(f, "vector_search_dimensions", None)
            if actual is not None and actual != expected_dim:
                raise ValueError(
                    f"Dimension mismatch: index '{index_name}' field "
                    f"'{vector_field}' is {actual}-dim but the embedding produces "
                    f"{expected_dim}-dim vectors. Use a matching embedding model "
                    f"or rebuild the index at {expected_dim} dims."
                )
            return
    logger.warning(
        "Vector field '%s' not found in index '%s' schema; proceeding — "
        "check field_map['vector'] matches your index.",
        vector_field, index_name,
    )


def _upload(client, docs: list[dict[str, Any]], result: ExportResult) -> None:
    """Upload one batch; record per-document outcome. The network call is the
    system edge — a batch error is caught and recorded, not raised, so one bad
    batch doesn't abort the whole export."""
    try:
        outcomes = client.upload_documents(documents=docs)
    except Exception as e:
        result.failed += len(docs)
        result.errors.append({"error": f"{type(e).__name__}: {e}", "count": len(docs)})
        logger.warning("Azure Search batch upload failed (%s): %s", type(e).__name__, e)
        return
    for o in outcomes:
        if getattr(o, "succeeded", False):
            result.uploaded += 1
        else:
            result.failed += 1
            result.errors.append({
                "key": getattr(o, "key", None),
                "error": getattr(o, "error_message", None),
            })


__all__ = ["push_to_azure_search", "ExportResult", "DEFAULT_FIELD_MAP"]
