"""
docingest.graph — optional GraphRAG layer on top of DocIngest's main pipeline.

This subpackage is intentionally NOT imported from ``docingest.__init__``: it
adds heavy optional dependencies (lightrag-hku, embedding clients) and reaches
beyond DocIngest's core "preprocess only" mandate. Users opt in by importing
``docingest.graph`` explicitly:

    import docingest
    import docingest.graph

    docingest.ingest("./docs/", output="./kb/")              # main pipeline
    docingest.graph.build("./kb/", mode="full")              # OPT-IN graph build
    answer = docingest.graph.query("...", knowledge_dir="./kb/", mode="hybrid")

Public surface:
    build, query, status                — top-level operations
    BuildResult, QueryResult, GraphStatus — return types
    EmbeddingProvider, OpenAIEmbedding, GeminiEmbedding,
        SentenceTransformerEmbedding   — credential / model injection
    GraphBackend                        — backend ABC (extension point for
                                          future MS GraphRAG / custom backends)

Everything else under ``docingest.graph.*`` is internal and may change between
releases without notice — same contract as ``docingest.pipeline`` / ``parsers``
/ ``chunkers`` for the main pipeline.

Optional dependency policy:
    The lightrag-hku package is required only when actually building or
    querying the graph. Importing ``docingest.graph`` itself does NOT import
    lightrag — the import is deferred into the LightRAG backend so that
    `docingest doctor`, tests, and dependency-listing tools work even when
    the ``[graph]`` extras are not installed.
"""

from .api import (
    build,
    query,
    status,
    enrich_chunks,
    BuildResult,
    QueryResult,
    GraphStatus,
)
from .enricher import EnrichResult
from .providers import (
    EmbeddingProvider,
    OpenAIEmbedding,
    GeminiEmbedding,
    SentenceTransformerEmbedding,
)
from .backends.base import GraphBackend

__all__ = [
    # Top-level operations
    "build",
    "query",
    "status",
    "enrich_chunks",
    # Result dataclasses
    "BuildResult",
    "QueryResult",
    "GraphStatus",
    "EnrichResult",
    # Embedding providers (credential / model injection)
    "EmbeddingProvider",
    "OpenAIEmbedding",
    "GeminiEmbedding",
    "SentenceTransformerEmbedding",
    # Backend extension point
    "GraphBackend",
]
