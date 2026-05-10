"""
Public Python API for the optional GraphRAG layer.

Three top-level operations: ``build`` / ``query`` / ``status``. Together
with the provider classes (``docingest.graph.providers``) and the result
dataclasses below, this is the STABLE surface other projects should
import. Backends and adapters under ``docingest.graph.backends`` /
``adapters`` / ``cache`` are internal and may change without notice.

Design echoes ``docingest.api`` for the main pipeline:

* Keyword-only signatures past the first positional argument.
* Provider injection via ``llm=...`` / ``embedding=...`` (Provider objects),
  with config / env-var fallback for callers who prefer YAML.
* ``config_overrides`` accepts both nested-dict and flat dot-path forms,
  fed through the same ``load_config`` four-layer merge as the main facade.
* Backend selection is config-driven (``graph.backend``) — the API stays
  agnostic and can grow new backends without changing this surface.

The graph layer NEVER initialises itself implicitly: ``import docingest``
won't load lightrag. Users must explicitly ``import docingest.graph`` and
call one of these three functions for any work to happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Union

from ..config import load_config, deep_merge, get_nested
from ..providers import VisionProvider, AudioProvider, TextProvider
from .providers import EmbeddingProvider
from .backends.base import GraphBackend, BuildOutcome, QueryOutcome, StatusOutcome

if TYPE_CHECKING:
    from .enricher import EnrichResult


# ---------------------------------------------------------------------------
# Public result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    """Returned by :func:`build`. Wraps backend's BuildOutcome plus context."""

    backend: str = ""
    mode: str = ""
    entities_count: int = 0
    relations_count: int = 0
    communities_count: int = 0
    chunks_processed: int = 0
    chunks_skipped_cached: int = 0
    elapsed_ms: int = 0
    output_dir: str = ""
    errors: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _from_outcome(
        cls,
        outcome: BuildOutcome,
        *,
        backend: str,
        mode: str,
        output_dir: Path,
    ) -> "BuildResult":
        return cls(
            backend=backend,
            mode=mode,
            entities_count=outcome.entities_count,
            relations_count=outcome.relations_count,
            communities_count=outcome.communities_count,
            chunks_processed=outcome.chunks_processed,
            chunks_skipped_cached=outcome.chunks_skipped_cached,
            elapsed_ms=outcome.elapsed_ms,
            output_dir=str(output_dir.resolve()),
            errors=list(outcome.errors),
            stats=dict(outcome.stats),
        )


@dataclass
class QueryResult:
    """Returned by :func:`query`. Mirrors backend's QueryOutcome with output_dir context."""

    answer: str = ""
    mode_used: str = ""
    elapsed_ms: int = 0
    context: list[dict[str, Any]] = field(default_factory=list)
    output_dir: str = ""
    stats: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _from_outcome(
        cls,
        outcome: QueryOutcome,
        *,
        output_dir: Path,
    ) -> "QueryResult":
        return cls(
            answer=outcome.answer,
            mode_used=outcome.mode_used,
            elapsed_ms=outcome.elapsed_ms,
            context=list(outcome.context),
            output_dir=str(output_dir.resolve()),
            stats=dict(outcome.stats),
        )


@dataclass
class GraphStatus:
    """Returned by :func:`status`. Pure inspection — no side effects."""

    built: bool = False
    backend: str = ""
    mode: str = ""
    entities_count: int = 0
    relations_count: int = 0
    communities_count: int = 0
    last_built_at: str | None = None
    embedding_model: str = ""
    embedding_dimension: int = 0
    output_dir: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _from_outcome(
        cls,
        outcome: StatusOutcome,
        *,
        output_dir: Path,
    ) -> "GraphStatus":
        return cls(
            built=outcome.built,
            backend=outcome.backend,
            mode=outcome.mode,
            entities_count=outcome.entities_count,
            relations_count=outcome.relations_count,
            communities_count=outcome.communities_count,
            last_built_at=outcome.last_built_at,
            embedding_model=outcome.embedding_model,
            embedding_dimension=outcome.embedding_dimension,
            output_dir=str(output_dir.resolve()),
            extras=dict(outcome.extras),
        )


# ---------------------------------------------------------------------------
# Provider arg type alias (matches docingest.api convention)
# ---------------------------------------------------------------------------

ProviderArg = Union[VisionProvider, AudioProvider, TextProvider, dict, None]
EmbeddingArg = Union[EmbeddingProvider, dict, None]


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------

def _build_graph_config(
    *,
    knowledge_dir: Path,
    mode: str | None,
    llm: ProviderArg,
    embedding: EmbeddingArg,
    config_overrides: dict[str, Any] | None,
    config_file: str | Path | None,
    force: bool | None,
) -> dict[str, Any]:
    """
    Build a full DocIngest config dict for a graph operation.

    Reuses ``load_config`` so the four-layer precedence (defaults < project
    YAML < env vars < overrides) applies uniformly across main pipeline
    and graph layer. The graph-specific fields live entirely under
    ``config["graph"]`` — main-pipeline config is untouched.

    Note: ``output.dir`` is set to ``knowledge_dir`` so the backend's
    relative-path conventions (graph.output_subdir, graph.cache.cache_dir)
    resolve correctly. This does NOT trigger main-pipeline behaviour
    (we never call run_pipeline from here).
    """
    layered: dict[str, Any] = {
        "output": {"dir": str(knowledge_dir)},
        "graph": {},
    }

    if mode is not None:
        layered["graph"]["mode"] = mode

    if force is True:
        # Per-call force flag piggybacks on the main pipeline's incremental
        # contract semantically, but the graph cache reads its own field
        # under graph.cache. We push it into both so users get the
        # intuitive "force = rebuild from scratch" behaviour regardless
        # of which knob the backend reads.
        layered.setdefault("incremental", {})["force"] = True
        layered["graph"]["force"] = True

    if llm is not None:
        _merge_llm_provider(layered, llm)

    if embedding is not None:
        _merge_embedding_provider(layered, embedding)

    if config_overrides:
        user = _normalize_overrides(config_overrides)
        layered = deep_merge(layered, user)

    return load_config(
        project_config_path=Path(config_file) if config_file else None,
        cli_overrides=layered,
    )


def _merge_llm_provider(layered: dict[str, Any], value: ProviderArg) -> None:
    """
    Merge an LLM Provider / dict into ``graph.llm``.

    Accepts the main DocIngest provider classes (Vision / Audio / Text) for
    convenience — they all expose ``.to_model_config()`` which yields the
    primary/fallback shape we want. Raw dicts are passed through verbatim.
    """
    if value is None:
        return
    if isinstance(value, (VisionProvider, AudioProvider, TextProvider)):
        payload = value.to_model_config()
    elif isinstance(value, dict):
        payload = value
    else:  # type: ignore[unreachable]
        raise TypeError(  # type: ignore[unreachable]
            f"graph.llm expects a Provider / dict / None, got {type(value).__name__}"
        )
    graph = layered.setdefault("graph", {})
    graph["llm"] = deep_merge(graph.get("llm", {}), payload)


def _merge_embedding_provider(
    layered: dict[str, Any],
    value: EmbeddingArg,
) -> None:
    """
    Merge an EmbeddingProvider / dict into ``graph.embedding``.

    EmbeddingProvider has no ``.to_model_config()`` (its config shape is
    flat, not primary/fallback); we serialise its dataclass fields directly.
    """
    if value is None:
        return
    payload: dict[str, Any]
    if isinstance(value, EmbeddingProvider):
        provider_obj: EmbeddingProvider = value
        payload = {
            "provider": provider_obj.provider,
            "model": provider_obj.model,
            "dimension": provider_obj.dimension,
            "max_token_size": provider_obj.max_token_size,
        }
        if provider_obj.api_key:
            payload["api_key"] = provider_obj.api_key
    elif isinstance(value, dict):
        payload = value
    else:  # type: ignore[unreachable]
        raise TypeError(  # type: ignore[unreachable]
            f"graph.embedding expects an EmbeddingProvider / dict / None, "
            f"got {type(value).__name__}"
        )
    graph = layered.setdefault("graph", {})
    graph["embedding"] = deep_merge(graph.get("embedding", {}), payload)


def _normalize_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Accept either nested or flat dot-path overrides (or a mix). Mirrors
    docingest.api._normalize_overrides exactly so callers experience the
    same shape across both facades.
    """
    nested: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(key, str) and "." in key:
            _set_dotted(nested, key, value)
        else:
            nested = deep_merge(nested, {key: value})
    return nested


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur = target
    for key in keys[:-1]:
        existing = cur.get(key)
        if not isinstance(existing, dict):
            cur[key] = {}
        cur = cur[key]
    cur[keys[-1]] = value


# ---------------------------------------------------------------------------
# Embedding resolution: explicit Provider object > config dict
# ---------------------------------------------------------------------------

def _resolve_embedding(
    config: dict[str, Any],
    explicit: EmbeddingArg,
) -> EmbeddingProvider:
    """
    Pick an EmbeddingProvider for this call.

    Order:
      1. Explicit Provider instance passed by the caller.
      2. Build one from ``config["graph"]["embedding"]``.
    """
    if isinstance(explicit, EmbeddingProvider):
        return explicit

    cfg = (config.get("graph") or {}).get("embedding") or {}
    provider = str(cfg.get("provider", "openai")).lower()
    common: dict[str, Any] = {
        "model": cfg.get("model", "text-embedding-3-small"),
        "api_key": cfg.get("api_key"),
        "dimension": int(cfg.get("dimension", 1536)),
        "max_token_size": int(cfg.get("max_token_size", 8192)),
    }

    # Lazy import the concrete classes so providers.py only loads when
    # actually building/querying — keeps `import docingest.graph` light.
    from .providers import OpenAIEmbedding, GeminiEmbedding, SentenceTransformerEmbedding

    if provider in ("openai",):
        return OpenAIEmbedding(provider="openai", **common)
    if provider in ("google", "gemini"):
        return GeminiEmbedding(provider="google", **common)
    if provider in ("sentence_transformers", "sentencetransformers", "local", "st"):
        return SentenceTransformerEmbedding(provider="sentence_transformers", **common)

    raise ValueError(
        f"Unsupported graph.embedding.provider '{provider}'. "
        f"Built-in providers: openai, google, sentence_transformers. "
        f"For others pass an EmbeddingProvider instance via embedding=...."
    )


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def _create_backend(
    config: dict[str, Any],
    embedding: EmbeddingProvider,
) -> GraphBackend:
    name = str(get_nested(config, "graph.backend", "lightrag")).lower()
    if name == "lightrag":
        try:
            from .backends.lightrag_backend import LightRAGBackend
        except ImportError as e:
            raise ImportError(
                "lightrag-hku is not installed. Install graph extras with:\n"
                "  pip install -e \".[graph]\"\n"
                f"  (underlying error: {e})"
            ) from e
        return LightRAGBackend(config, embedding)

    raise ValueError(
        f"Unknown graph.backend '{name}'. Built-in backends: lightrag."
    )


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def build(
    knowledge_dir: str | Path,
    *,
    mode: str | None = None,
    llm: ProviderArg = None,
    embedding: EmbeddingArg = None,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
    force: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> BuildResult:
    """
    Build (or incrementally extend) the knowledge graph for a knowledge base.

    The knowledge base must already be produced by ``docingest.ingest`` or
    the CLI ``docingest run`` — this function reads ``chunks.jsonl`` and
    never touches the original documents.

    Args:
        knowledge_dir: Knowledge-base root (the ``output_dir`` you passed
            to ``docingest.ingest``). The graph is written under
            ``knowledge_dir / config.graph.output_subdir`` (default
            ``graph/``).
        mode: Override ``graph.mode``. ``"vector_only"`` skips global /
            hybrid query support to save cost; ``"full"`` enables every
            LightRAG retrieval mode.
        llm: LLM provider for entity extraction + community summaries.
            Accepts a DocIngest provider (e.g. ``GeminiProvider(api_key=...)``)
            or a raw config dict, or None to keep YAML / env defaults.
        embedding: Embedding provider. Accepts an ``EmbeddingProvider``
            instance, a raw config dict, or None for YAML defaults.
        config_overrides: Free-form overrides (nested dict OR flat dot-path
            OR mix). Same semantics as ``docingest.ingest``.
        config_file: Path to a project ``docingest.yaml``.
        force: Ignore the graph-level extraction cache and rebuild from
            scratch. Does NOT remove the LightRAG working_dir contents —
            re-extraction overwrites in place.
        on_progress: Optional ``callback(event_dict)`` fired per chunk.
            Event schema: ``{"current", "total", "chunk_id", "status",
            "error"}`` with status one of ``extracted | cached | skipped |
            failed``. Exceptions inside the callback are swallowed
            (logged at warning level) so a buggy callback can't break
            the build.

    Returns:
        :class:`BuildResult` with counts, elapsed time, and any non-fatal
        errors encountered during the run.
    """
    knowledge_path = Path(knowledge_dir)
    config = _build_graph_config(
        knowledge_dir=knowledge_path,
        mode=mode,
        llm=llm,
        embedding=embedding,
        config_overrides=config_overrides,
        config_file=config_file,
        force=force,
    )

    embedding_resolved = _resolve_embedding(config, embedding)
    backend = _create_backend(config, embedding_resolved)

    chunks_file = get_nested(config, "graph.input.chunks_file", "chunks.jsonl")
    chunks_path = knowledge_path / chunks_file

    outcome = backend.build(
        chunks_path=chunks_path,
        output_dir=knowledge_path,
        force=force,
        on_progress=on_progress,
    )

    build_result = BuildResult._from_outcome(
        outcome,
        backend=str(get_nested(config, "graph.backend", "lightrag")),
        mode=str(get_nested(config, "graph.mode", "full")),
        output_dir=knowledge_path,
    )

    # Optional follow-up: chunk enrichment. Triggered by config flag
    # (graph.enrich_chunks.enabled) so users opt in once and forget; CLI
    # callers also force-enable via --enrich-chunks. Failures inside the
    # enricher are recorded but never propagate as build failure — the
    # graph itself was built successfully and is still usable.
    if get_nested(config, "graph.enrich_chunks.enabled", False) and not outcome.errors:
        from .enricher import enrich as _enrich
        enrich_result = _enrich(knowledge_path, config)
        build_result.stats["enriched_chunks_written"] = enrich_result.chunks_enriched
        build_result.stats["enriched_total_entities_injected"] = (
            enrich_result.total_entities_injected
        )
        build_result.stats["enriched_avg_entities_per_chunk"] = (
            enrich_result.avg_entities_per_chunk
        )
        build_result.stats["enriched_output_path"] = enrich_result.written_path
        if enrich_result.errors:
            build_result.errors.extend(
                f"enrich_chunks: {msg}" for msg in enrich_result.errors
            )

    return build_result


def enrich_chunks(
    knowledge_dir: str | Path,
    *,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
) -> "EnrichResult":
    """
    Generate ``chunks_enriched.jsonl`` from a previously-built graph.

    Standalone counterpart to ``build`` — useful when you want to enrich
    after the graph was built (perhaps from an earlier run that didn't
    set ``graph.enrich_chunks.enabled = true``). Runs the enricher on
    the existing graph artefacts; never invokes any LLM / embedding
    client.

    The original ``chunks.jsonl`` is NEVER modified — the enricher writes
    a sibling ``chunks_enriched.jsonl`` (filename configurable via
    ``graph.enrich_chunks.output_file``). Re-running overwrites that
    sibling atomically.

    Args:
        knowledge_dir: Knowledge-base root.
        config_overrides / config_file: Same semantics as :func:`build`.

    Returns:
        :class:`EnrichResult` with counts, elapsed time, and any soft
        errors. NEVER raises.
    """
    from .enricher import enrich as _enrich

    knowledge_path = Path(knowledge_dir)
    # Force the toggle on for this single call so downstream code paths
    # that branch on graph.enrich_chunks.enabled (none today, but future-
    # proof) see the right state.
    overrides = dict(config_overrides or {})
    overrides.setdefault("graph", {})
    if isinstance(overrides["graph"], dict):
        overrides["graph"].setdefault("enrich_chunks", {})
        if isinstance(overrides["graph"]["enrich_chunks"], dict):
            overrides["graph"]["enrich_chunks"]["enabled"] = True

    config = _build_graph_config(
        knowledge_dir=knowledge_path,
        mode=None,
        llm=None,
        embedding=None,
        config_overrides=overrides,
        config_file=config_file,
        force=None,
    )
    return _enrich(knowledge_path, config)


def query(
    question: str,
    *,
    knowledge_dir: str | Path,
    mode: str = "hybrid",
    top_k: int | None = None,
    return_context: bool = False,
    llm: ProviderArg = None,
    embedding: EmbeddingArg = None,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
    extra: dict[str, Any] | None = None,
) -> QueryResult:
    """
    Query a previously-built graph.

    Args:
        query: Natural-language question.
        knowledge_dir: Knowledge-base root used at build time.
        mode: Retrieval mode. For LightRAG: ``"naive"`` (pure vector RAG),
            ``"local"`` (entity-anchored), ``"global"`` (community
            summaries), ``"hybrid"`` (local + global), ``"mix"``
            (everything). When the graph was built with mode=vector_only,
            only ``naive`` and ``local`` are accepted.
        top_k: Backend-specific retrieval cutoff. None = backend default.
        return_context: When True, ask the backend to surface retrieved
            evidence alongside the answer (when supported).
        llm: LLM provider — must match the build config's LLM in shape so
            answer rendering uses the model the user expects. Defaults to
            YAML config.
        embedding: Embedding provider. The provider's ``model`` and
            ``dimension`` MUST match the build-time embedding (LightRAG's
            vectors are baked at build time). Mismatches surface as a
            backend error rather than silent corruption.
        config_overrides / config_file: same semantics as :func:`build`.
        extra: Backend-specific knobs not on the typed surface
            (LightRAG: ``response_type`` / ``user_prompt`` / ``chunk_top_k``
            / ``enable_rerank``). Pass a dict; backends ignore unknown keys.

    Returns:
        :class:`QueryResult`.
    """
    knowledge_path = Path(knowledge_dir)
    config = _build_graph_config(
        knowledge_dir=knowledge_path,
        mode=None,                 # mode here is a query mode, NOT graph.mode
        llm=llm,
        embedding=embedding,
        config_overrides=config_overrides,
        config_file=config_file,
        force=None,
    )

    embedding_resolved = _resolve_embedding(config, embedding)
    backend = _create_backend(config, embedding_resolved)

    backend_extra = dict(extra or {})
    backend_extra["output_dir"] = str(knowledge_path)

    outcome = backend.query(
        question,
        mode=mode,
        top_k=top_k,
        return_context=return_context,
        extra=backend_extra,
    )

    return QueryResult._from_outcome(outcome, output_dir=knowledge_path)


def status(
    knowledge_dir: str | Path,
    *,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
) -> GraphStatus:
    """
    Inspect a knowledge base's graph build state.

    Cheap — never invokes LLM or embedding clients, never imports the
    chosen backend's heavy dependencies. Reads the manifest + counts
    on-disk artefacts and returns. Safe to call against directories that
    were never graph-built (``GraphStatus.built == False``).
    """
    knowledge_path = Path(knowledge_dir)
    config = _build_graph_config(
        knowledge_dir=knowledge_path,
        mode=None,
        llm=None,
        embedding=None,
        config_overrides=config_overrides,
        config_file=config_file,
        force=None,
    )
    output_subdir = str(get_nested(config, "graph.output_subdir", "graph"))

    backend_name = str(get_nested(config, "graph.backend", "lightrag")).lower()
    if backend_name == "lightrag":
        # Use the dir-driven helper so we don't pay for LightRAG import
        # just to print "not built".
        from .backends.lightrag_backend import status_from_dir
        outcome = status_from_dir(knowledge_path, output_subdir=output_subdir)
    else:
        # Future backends: instantiate and call status() — but we don't
        # have any yet, so error explicitly rather than silently degrade.
        raise ValueError(
            f"status() not implemented for graph.backend='{backend_name}'."
        )

    return GraphStatus._from_outcome(outcome, output_dir=knowledge_path)


__all__ = [
    "build",
    "query",
    "status",
    "enrich_chunks",
    "BuildResult",
    "QueryResult",
    "GraphStatus",
]
