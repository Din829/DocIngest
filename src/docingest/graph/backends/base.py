"""
GraphBackend abstract base class.

The interface is intentionally minimal — three methods (build / query /
status) — for two reasons:

1. **Don't over-abstract before we have two implementations.** The first
   backend is LightRAG; it already covers entity extraction, relation
   extraction, community detection, and three retrieval modes internally.
   If a future backend (Microsoft GraphRAG, custom) needs finer-grained
   extension points, we add them when the second backend exists, not before.

2. **Match the shape of DocIngest's existing extension points.** Parsers
   and chunkers in the main pipeline expose similarly small ABCs; this
   module follows the same convention so contributors don't have to learn
   a different style for the graph layer.

Backend implementations live in ``backends/<name>_backend.py``. The factory
in ``api.py`` selects one based on ``graph.backend`` config.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Shared dataclasses returned by backend methods
# ---------------------------------------------------------------------------

@dataclass
class BuildOutcome:
    """
    Returned by ``GraphBackend.build``.

    Backends populate as much as they can. ``stats`` carries per-backend
    extras (e.g. LightRAG returns counts of LLM calls and cache hits) that
    don't belong in the typed surface.
    """

    entities_count: int = 0
    relations_count: int = 0
    communities_count: int = 0
    chunks_processed: int = 0
    chunks_skipped_cached: int = 0
    elapsed_ms: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class QueryOutcome:
    """
    Returned by ``GraphBackend.query``.

    ``answer`` is the rendered natural-language response. ``context`` is
    the retrieved evidence (entity / relation / chunk fragments) — backends
    populate it when ``return_context`` was set on the query call. Empty
    list means either nothing matched or the backend doesn't expose
    intermediate context.
    """

    answer: str = ""
    context: list[dict[str, Any]] = field(default_factory=list)
    mode_used: str = ""
    elapsed_ms: int = 0
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusOutcome:
    """Returned by ``GraphBackend.status``."""

    built: bool = False
    backend: str = ""
    mode: str = ""
    entities_count: int = 0
    relations_count: int = 0
    communities_count: int = 0
    last_built_at: str | None = None
    embedding_model: str = ""
    embedding_dimension: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class GraphBackend(ABC):
    """
    Abstract base for GraphRAG backends.

    Subclasses are constructed by ``api.py`` with a fully-resolved config
    and the relevant providers already injected. They own their working
    directory layout under ``output_dir / graph.output_subdir``; DocIngest
    never touches files there directly.
    """

    @abstractmethod
    def build(
        self,
        chunks_path: Path,
        output_dir: Path,
        *,
        force: bool = False,
        on_progress: "Any | None" = None,
    ) -> BuildOutcome:
        """
        Build (or incrementally extend) the graph from a chunks.jsonl file.

        Args:
            chunks_path: Absolute path to chunks.jsonl produced by the main
                pipeline. Backends MUST NOT re-parse original documents.
            output_dir: Knowledge base root. Backends create / read files
                under ``output_dir / config.graph.output_subdir``.
            force: When True, ignore extraction cache and rebuild from
                scratch. Default False — incremental updates only re-extract
                chunks whose content or LLM config has changed.
            on_progress: Optional callable invoked once per processed chunk
                with a dict ``{"current": int, "total": int, "chunk_id": str,
                "status": "extracted" | "cached" | "skipped" | "failed",
                "error": str | None}``. Exceptions raised from the callback
                are swallowed so a buggy callback can't break the build.

        Returns:
            BuildOutcome with counts and elapsed time. Backends record
            partial errors in ``outcome.errors`` and continue rather than
            aborting the whole build.
        """

    @abstractmethod
    def query(
        self,
        query: str,
        *,
        mode: str,
        top_k: int | None = None,
        return_context: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> QueryOutcome:
        """
        Query the built graph.

        Args:
            query: Natural-language question.
            mode: Backend-specific retrieval mode. For LightRAG one of
                ``"naive" | "local" | "global" | "hybrid" | "mix"``. The
                facade in ``api.py`` validates the mode against the build
                mode (``vector_only`` builds reject ``"global"`` etc.) so
                backends can assume valid input.
            top_k: Backend-specific retrieval cutoff. None = backend default.
            return_context: When True, populate ``QueryOutcome.context``
                with the retrieved evidence fragments. Off by default to
                keep the response payload small.
            extra: Backend-specific knobs that don't fit the typed surface
                (e.g. LightRAG's ``response_type`` / ``user_prompt``). Pass
                a dict; backends ignore unknown keys.

        Returns:
            QueryOutcome with the rendered answer and optional context.
        """

    @abstractmethod
    def status(self) -> StatusOutcome:
        """
        Report whether the graph is built and basic statistics.

        Must be cheap (no LLM calls, no embedding work) — used by status
        commands and CLI / MCP to render dashboards. Returning ``built=False``
        is a normal state, not an error: it simply means no graph exists at
        the configured location yet.
        """


__all__ = [
    "GraphBackend",
    "BuildOutcome",
    "QueryOutcome",
    "StatusOutcome",
]
