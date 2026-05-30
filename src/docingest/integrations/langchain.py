# -*- coding: utf-8 -*-
"""
LangChain integration — load DocIngest chunks as LangChain Documents.

DocIngest already produces high-quality, semantics-aware chunks (heading /
slide / table boundaries, title_path injection, lineage) in chunks.jsonl.
This loader hands those straight to LangChain as ``Document`` objects, so a
downstream app REUSES DocIngest's chunking instead of re-splitting the
markdown with a naive character splitter.

Because LangChain itself integrates dozens of vector stores / retrievers
(Azure AI Search, Amazon Bedrock Knowledge Bases, Pinecone, OpenSearch, ...),
this one adapter bridges DocIngest to all of them:

    DocIngest chunks.jsonl  ->  Document  ->  any LangChain vector store

Usage::

    from docingest.integrations.langchain import DocIngestLoader

    docs = DocIngestLoader("knowledge/my_kb").load()   # or stream via .lazy_load()
    vectorstore.add_documents(docs)

Opt-in: needs ``langchain-core`` (``pip install 'docingest[langchain]'``).
The core ``docingest run`` pipeline never imports this module.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

try:
    from langchain_core.document_loaders import BaseLoader
    from langchain_core.documents import Document
except ImportError as e:  # pragma: no cover - only hit without the extra
    raise ImportError(
        "DocIngestLoader requires langchain-core. "
        "Install it with:  pip install 'docingest[langchain]'"
    ) from e


class DocIngestLoader(BaseLoader):
    """
    Load a DocIngest knowledge base's chunks.jsonl as LangChain Documents.

    Each chunk maps 1:1 to a Document::

        Document(id=chunk_id, page_content=chunk_text, metadata=chunk_metadata)

    The text already carries DocIngest's ``title_path`` header and the metadata
    carries source / format / title_path / lineage / has_table …, so downstream
    retrieval keeps full provenance with zero re-processing. Only ``lazy_load``
    is implemented; LangChain's BaseLoader derives ``load`` / ``aload`` /
    ``alazy_load`` from it.

    Args:
        path: A knowledge dir (containing chunks.jsonl) OR a direct path to a
            chunks.jsonl file.
        formats: Optional lowercase format whitelist (e.g. ``["pdf", "docx"]``).
            None / empty keeps every chunk.
        min_chunk_tokens: Drop chunks whose recorded ``metadata["tokens"]`` is
            below this (reuses the count DocIngest already computed). 0 = off.
        chunks_filename: Override the file name when ``path`` is a directory.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        formats: list[str] | None = None,
        min_chunk_tokens: int = 0,
        chunks_filename: str = "chunks.jsonl",
    ) -> None:
        p = Path(path)
        self.chunks_path = p / chunks_filename if p.is_dir() else p
        self._formats = {f.lower() for f in formats} if formats else None
        self._min_tokens = int(min_chunk_tokens)

    def lazy_load(self) -> Iterator[Document]:
        """
        Stream chunks.jsonl, yielding one Document per chunk.

        Malformed lines are skipped silently — a single bad line must not abort
        a large load (same policy as DocIngest's own chunk readers).
        """
        if not self.chunks_path.exists():
            raise FileNotFoundError(f"chunks.jsonl not found: {self.chunks_path}")

        with open(self.chunks_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                chunk_id = record.get("id")
                text = record.get("text")
                metadata = record.get("metadata") or {}
                if not chunk_id or not isinstance(text, str) or not text.strip():
                    continue

                # Filters reuse the metadata DocIngest already computed.
                if self._formats is not None:
                    if str(metadata.get("format", "")).lower() not in self._formats:
                        continue
                if self._min_tokens > 0 and int(metadata.get("tokens", 0)) < self._min_tokens:
                    continue

                yield Document(
                    id=str(chunk_id),
                    page_content=text,
                    metadata=metadata,
                )


__all__ = ["DocIngestLoader"]
