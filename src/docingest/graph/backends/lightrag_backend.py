"""
LightRAG backend — concrete ``GraphBackend`` implementation.

Wraps ``lightrag.LightRAG`` end-to-end:
    build()  → await rag.initialize_storages(); await rag.ainsert(...)
    query()  → await rag.aquery(question, param=QueryParam(...))
    status() → reads the working_dir layout LightRAG produces

Design notes
------------
* Async core, sync façade. ``GraphBackend.build`` / ``query`` / ``status``
  are sync (the rest of DocIngest is sync); we drive LightRAG's async API
  via ``asyncio.run`` for one-shot CLI / MCP / library calls. If a future
  caller is already inside an event loop they can use
  ``LightRAGBackend.async_build`` / ``async_query`` directly.

* Lazy lightrag import. The import happens inside the methods, never at
  module load. This keeps ``import docingest.graph`` working without the
  ``[graph]`` extras installed (e.g. for ``docingest doctor``, for tests
  that mock the backend, for users who only use the providers module).

* Mode mapping:
      DocIngest config        →   LightRAG behaviour at build / query
      ─────────────────────────────────────────────────────────────────
      graph.mode = vector_only →  build still runs LightRAG's full
                                  insert (LightRAG always builds entities
                                  + relations); query restricts mode to
                                  ``naive`` / ``local`` only. Community
                                  detection still runs internally — we
                                  just refuse to expose ``global`` /
                                  ``hybrid`` / ``mix`` to callers, since
                                  the user opted out of paying for those.
                                  (Note: there's no LightRAG flag to
                                  skip community summary generation; the
                                  cost lives in build either way.)
      graph.mode = full       →  every LightRAG query mode allowed.

* Cache integration. We consult the chunk-level cache BEFORE calling
  ``ainsert``, so unchanged chunks bypass LightRAG entirely. LightRAG's
  own LLM-prompt cache (``enable_llm_cache``) catches anything that does
  reach it. The two caches compose; neither replaces the other.

References
----------
LightRAG public API surface used here (verified against the official
ProgramingWithCore.md):
    LightRAG(working_dir, llm_model_func, embedding_func, addon_params,
             chunk_token_size, chunk_overlap_token_size,
             entity_extract_max_gleaning, enable_llm_cache,
             kv_storage, vector_storage, graph_storage)
    await rag.initialize_storages()      # MUST be called before insert/query
    await rag.ainsert(text_or_list, ids=..., file_paths=...)
    await rag.aquery(question, param=QueryParam(mode=...))
    await rag.finalize_storages()         # release file handles / connections
    QueryParam(mode, top_k, only_need_context, response_type, ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..adapters.chunks_loader import LoadedChunk, load_all
from ..adapters.llm_adapter import make_lightrag_llm_func
from ..providers import EmbeddingProvider
from .. import cache as graph_cache
from .base import (
    GraphBackend,
    BuildOutcome,
    QueryOutcome,
    StatusOutcome,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mode → allowed LightRAG query modes
# ---------------------------------------------------------------------------

# vector_only: the user opted out of paying for the full graph traversal
# layer; restrict queries to LightRAG's vector-style modes. ("naive" is
# pure vector RAG; "local" is entity-anchored vector retrieval — both are
# the cheap end of LightRAG's spectrum.)
_VECTOR_ONLY_QUERY_MODES = frozenset({"naive", "local"})

# full: every LightRAG mode is on the table.
_ALL_QUERY_MODES = frozenset({"naive", "local", "global", "hybrid", "mix"})


def _validate_query_mode(build_mode: str, query_mode: str) -> str:
    """
    Validate / normalise the user-supplied query mode.

    Returns the lowercased mode string when valid; raises ValueError with
    a helpful message otherwise.
    """
    normalised = query_mode.strip().lower()
    if build_mode == "vector_only":
        if normalised not in _VECTOR_ONLY_QUERY_MODES:
            raise ValueError(
                f"Query mode '{query_mode}' requires graph.mode='full'. "
                f"This graph was built with mode='vector_only', which only "
                f"supports {sorted(_VECTOR_ONLY_QUERY_MODES)}. Either rebuild "
                f"with graph.mode='full' or pick a supported mode."
            )
    else:
        if normalised not in _ALL_QUERY_MODES:
            raise ValueError(
                f"Unknown query mode '{query_mode}'. "
                f"Valid modes: {sorted(_ALL_QUERY_MODES)}."
            )
    return normalised


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class LightRAGBackend(GraphBackend):
    """
    LightRAG-backed implementation of ``GraphBackend``.

    Construct with a fully-resolved DocIngest config + an EmbeddingProvider.
    The LLM provider config is read from ``config["graph"]["llm"]`` and
    pushed through DocIngest's existing ``models/provider.py`` machinery
    via the ``llm_adapter`` — no separate LLM client lives here.
    """

    BACKEND_NAME = "lightrag"

    def __init__(
        self,
        config: dict[str, Any],
        embedding: EmbeddingProvider,
    ) -> None:
        self.config = config
        self.embedding = embedding
        self.graph_cfg: dict[str, Any] = config.get("graph", {}) or {}
        self.lightrag_cfg: dict[str, Any] = self.graph_cfg.get("lightrag", {}) or {}
        self.mode: str = str(self.graph_cfg.get("mode", "full")).lower()

    # ----- working_dir layout -----------------------------------------------

    def _resolve_working_dir(self, output_dir: Path) -> Path:
        """
        ``output_dir`` is the knowledge-base root; LightRAG's working_dir
        sits at ``output_dir / graph.output_subdir`` so all graph artefacts
        live under one deletable folder, matching DocIngest's "outputs are
        files" philosophy.
        """
        subdir = self.graph_cfg.get("output_subdir", "graph")
        return output_dir / subdir

    def _resolve_cache_dir(self, output_dir: Path) -> Path:
        cache_subdir = (self.graph_cfg.get("cache") or {}).get(
            "cache_dir", ".graph_cache"
        )
        return output_dir / cache_subdir

    # ----- LightRAG instance construction -----------------------------------

    def _build_lightrag(self, working_dir: Path, output_dir: Path) -> Any:
        """
        Construct (but do not initialise storages for) a LightRAG instance.

        Caller MUST ``await rag.initialize_storages()`` before insert/query
        and ``await rag.finalize_storages()`` when done — this is LightRAG's
        documented contract; failing to do so leaves file locks held.

        ``output_dir`` is the knowledge-base root — used to resolve
        index.json for ``language: "auto"`` detection. (working_dir is
        the graph subfolder; index.json sits one level up.)
        """
        # Lazy import — see module docstring.
        from lightrag import LightRAG  # type: ignore[import-not-found]

        working_dir.mkdir(parents=True, exist_ok=True)

        llm_func = make_lightrag_llm_func(self.graph_cfg.get("llm", {}) or {})
        embedding_func = self.embedding.to_lightrag_func()

        # addon_params accepts at minimum {"language": ..., "entity_types": ...}
        # per LightRAG's prompt template engine. We never set entity_types
        # by default — LightRAG's defaults (organization / person /
        # location / event / ...) cover general documents well; users who
        # need domain-specific types pass them via config_overrides.
        addon_params: dict[str, Any] = {}
        language_cfg = self.lightrag_cfg.get("language", "auto")
        resolved_language = _resolve_language(language_cfg, output_dir)
        if resolved_language:
            addon_params["language"] = resolved_language

        entity_types = self.lightrag_cfg.get("entity_types")
        if entity_types:
            addon_params["entity_types"] = entity_types

        # Construct with only the parameters LightRAG documents. Unknown
        # kwargs would raise — better to fail loud at install time than to
        # silently misconfigure.
        kwargs: dict[str, Any] = {
            "working_dir": str(working_dir),
            "llm_model_func": llm_func,
            "embedding_func": embedding_func,
            "chunk_token_size": int(self.lightrag_cfg.get("chunk_token_size", 1200)),
            "chunk_overlap_token_size": int(
                self.lightrag_cfg.get("chunk_overlap_token_size", 100)
            ),
            "entity_extract_max_gleaning": int(
                self.lightrag_cfg.get("entity_extract_max_gleaning", 1)
            ),
            "enable_llm_cache": bool(
                self.lightrag_cfg.get("enable_llm_cache", True)
            ),
        }
        if addon_params:
            kwargs["addon_params"] = addon_params

        return LightRAG(**kwargs)

    # ----- public sync surface (GraphBackend ABC) ---------------------------

    def build(
        self,
        chunks_path: Path,
        output_dir: Path,
        *,
        force: bool = False,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> BuildOutcome:
        return asyncio.run(
            self.async_build(
                chunks_path,
                output_dir,
                force=force,
                on_progress=on_progress,
            )
        )

    def query(
        self,
        query: str,
        *,
        mode: str,
        top_k: int | None = None,
        return_context: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> QueryOutcome:
        return asyncio.run(
            self.async_query(
                query,
                mode=mode,
                top_k=top_k,
                return_context=return_context,
                extra=extra,
            )
        )

    def status(self) -> StatusOutcome:
        # status reads files from the LAST built knowledge base. Without a
        # build call there's no working_dir context, so the caller is
        # expected to set ``self._last_output_dir`` via async_build, OR to
        # construct the backend with the output_dir already encoded —
        # which we don't, to keep the constructor side-effect free.
        # Instead, status() always returns "not built" when called on a
        # fresh instance; the api.py facade calls a separate code path
        # (``_status_from_dir``) that does the file inspection.
        return StatusOutcome(
            built=False,
            backend=self.BACKEND_NAME,
            mode=self.mode,
            extras={
                "note": (
                    "LightRAGBackend.status() on a bare instance returns the "
                    "no-build sentinel. Use docingest.graph.status() at the "
                    "facade level for a directory-driven check."
                ),
            },
        )

    # ----- async core -------------------------------------------------------

    async def async_build(
        self,
        chunks_path: Path,
        output_dir: Path,
        *,
        force: bool = False,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> BuildOutcome:
        t_start = time.monotonic()
        outcome = BuildOutcome()

        if not chunks_path.exists():
            outcome.errors.append(
                f"chunks file not found: {chunks_path} — run `docingest run` first"
            )
            outcome.elapsed_ms = int((time.monotonic() - t_start) * 1000)
            return outcome

        working_dir = self._resolve_working_dir(output_dir)
        cache_dir = self._resolve_cache_dir(output_dir)

        # 1) load chunks (streamed in load_all → list for progress totals)
        formats = (self.graph_cfg.get("input") or {}).get("formats")
        min_tokens = int(
            (self.graph_cfg.get("input") or {}).get("min_chunk_tokens", 0)
        )
        chunks: list[LoadedChunk] = load_all(
            chunks_path,
            formats=formats,
            min_chunk_tokens=min_tokens,
        )

        # 2) cache check
        cache_enabled = bool((self.graph_cfg.get("cache") or {}).get("enabled", True))
        if force:
            entries: dict[str, dict[str, Any]] = {}
        else:
            entries = graph_cache.load_state(cache_dir) if cache_enabled else {}
        llm_hash = graph_cache.llm_config_hash(self.config)

        # Partition into to-extract vs cached, in chunks order so progress
        # events stream monotonically.
        to_extract: list[LoadedChunk] = []
        for c in chunks:
            ch_hash = graph_cache.chunk_content_hash(c.original_text)
            if (
                cache_enabled
                and not force
                and graph_cache.is_chunk_cached(entries, c.chunk_id, ch_hash, llm_hash)
            ):
                outcome.chunks_skipped_cached += 1
                _emit(on_progress, {
                    "current": outcome.chunks_processed + outcome.chunks_skipped_cached,
                    "total": len(chunks),
                    "chunk_id": c.chunk_id,
                    "status": "cached",
                    "error": None,
                })
                continue
            to_extract.append(c)

        # 2.5) force-rebuild scrub: --force semantically means "rebuild
        # from scratch", so the LightRAG working_dir's prior artefacts
        # must go too. Re-feeding the same chunk_ids into a populated
        # LightRAG store yields ill-defined behaviour (duplicate entities
        # vs. update vs. silent skip — depends on the storage backend).
        # Wiping ensures the rebuild is genuinely from-scratch.
        if force and working_dir.exists():
            for child in working_dir.iterdir():
                # Spare the manifest until we write a fresh one — preserves
                # the "graph existed at all" signal during the gap.
                if child.name == self._MANIFEST_NAME:
                    continue
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except OSError as e:
                    logger.warning(f"force-scrub could not remove {child}: {e}")

        # 3) feed the new chunks into LightRAG
        if to_extract:
            rag = self._build_lightrag(working_dir, output_dir)
            try:
                await rag.initialize_storages()
            except Exception as e:
                outcome.errors.append(f"initialize_storages failed: {e}")
                outcome.elapsed_ms = int((time.monotonic() - t_start) * 1000)
                return outcome

            try:
                texts = [c.text for c in to_extract]
                ids = [c.chunk_id for c in to_extract]
                file_paths = [c.source_file or c.chunk_id for c in to_extract]

                # ainsert is documented as accepting str-or-list; we always
                # pass list form for batch behaviour. LightRAG handles its
                # own internal concurrency via max_parallel_insert.
                try:
                    await rag.ainsert(texts, ids=ids, file_paths=file_paths)
                except Exception as e:
                    # Single hard failure on the whole batch — record and
                    # bail out without persisting any cache updates so the
                    # next run retries from scratch.
                    outcome.errors.append(f"ainsert failed: {e}")
                    return outcome

                # On success, record every extracted chunk in the cache.
                for c in to_extract:
                    ch_hash = graph_cache.chunk_content_hash(c.original_text)
                    graph_cache.mark_extracted(
                        entries, c.chunk_id, ch_hash, llm_hash
                    )
                    outcome.chunks_processed += 1
                    _emit(on_progress, {
                        "current": outcome.chunks_processed + outcome.chunks_skipped_cached,
                        "total": len(chunks),
                        "chunk_id": c.chunk_id,
                        "status": "extracted",
                        "error": None,
                    })
            finally:
                # Best-effort cleanup — LightRAG holds file handles open.
                try:
                    await rag.finalize_storages()
                except Exception as e:
                    logger.warning(f"finalize_storages raised, ignoring: {e}")

        # 4) persist cache state (even when nothing was extracted, so a
        #    later --force followed by a normal run sees the right baseline)
        if cache_enabled:
            try:
                graph_cache.save_state(cache_dir, entries)
            except OSError as e:
                logger.warning(f"Could not persist graph cache: {e}")

        # 5) record build manifest for status()
        try:
            self._write_manifest(working_dir, len(chunks), outcome)
        except OSError as e:
            logger.warning(f"Could not write graph manifest: {e}")

        # 6) tally entity / relation / community counts from LightRAG's
        #    on-disk artefacts so the outcome carries useful stats even
        #    when most chunks were cache hits.
        counts = _count_lightrag_artefacts(working_dir)
        outcome.entities_count = counts.get("entities", 0)
        outcome.relations_count = counts.get("relations", 0)
        outcome.communities_count = counts.get("communities", 0)
        outcome.stats.update(counts)

        outcome.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return outcome

    async def async_query(
        self,
        query: str,
        *,
        mode: str,
        top_k: int | None = None,
        return_context: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> QueryOutcome:
        from lightrag import QueryParam  # type: ignore[import-not-found]

        normalised_mode = _validate_query_mode(self.mode, mode)
        t_start = time.monotonic()

        # We need an output_dir for working_dir resolution; the facade
        # passes it via ``extra["output_dir"]``. The backend doesn't keep
        # output_dir on self (build() takes it as a param) so the query
        # path needs the same explicit hand-off.
        extra = extra or {}
        output_dir_raw = extra.get("output_dir")
        if output_dir_raw is None:
            raise ValueError(
                "LightRAGBackend.async_query requires extra['output_dir'] — "
                "the facade passes this; if you're calling the backend "
                "directly, supply it yourself."
            )
        output_dir = Path(output_dir_raw)
        working_dir = self._resolve_working_dir(output_dir)

        if not working_dir.exists():
            return QueryOutcome(
                answer="",
                mode_used=normalised_mode,
                elapsed_ms=int((time.monotonic() - t_start) * 1000),
                stats={"error": "graph not built — run `docingest graph build` first"},
            )

        rag = self._build_lightrag(working_dir, output_dir)
        try:
            await rag.initialize_storages()
        except Exception as e:
            return QueryOutcome(
                answer="",
                mode_used=normalised_mode,
                elapsed_ms=int((time.monotonic() - t_start) * 1000),
                stats={"error": f"initialize_storages failed: {e}"},
            )

        try:
            param_kwargs: dict[str, Any] = {"mode": normalised_mode}
            if top_k is not None:
                param_kwargs["top_k"] = int(top_k)
            if return_context:
                param_kwargs["only_need_context"] = True
            # Forward whitelisted extras to QueryParam (response_type and
            # user_prompt are the two most useful per LightRAG's docs).
            for key in ("response_type", "user_prompt", "chunk_top_k", "enable_rerank"):
                if key in extra:
                    param_kwargs[key] = extra[key]
            param = QueryParam(**param_kwargs)

            try:
                answer = await rag.aquery(query, param=param)
            except Exception as e:
                return QueryOutcome(
                    answer="",
                    mode_used=normalised_mode,
                    elapsed_ms=int((time.monotonic() - t_start) * 1000),
                    stats={"error": f"aquery failed: {e}"},
                )

            return QueryOutcome(
                answer=str(answer) if answer is not None else "",
                context=[],   # LightRAG bundles context into the answer text
                              # when only_need_context=True; we forward as-is
                              # in `answer` for now (Phase 2 can split it).
                mode_used=normalised_mode,
                elapsed_ms=int((time.monotonic() - t_start) * 1000),
            )
        finally:
            try:
                await rag.finalize_storages()
            except Exception as e:
                logger.warning(f"finalize_storages raised, ignoring: {e}")

    # ----- manifest (used by api.status) ------------------------------------

    _MANIFEST_NAME = "docingest_graph.json"

    def _write_manifest(
        self,
        working_dir: Path,
        total_chunks: int,
        outcome: BuildOutcome,
    ) -> None:
        manifest = {
            "version": 1,
            "backend": self.BACKEND_NAME,
            "mode": self.mode,
            "embedding": {
                "provider": self.embedding.provider,
                "model": self.embedding.model,
                "dimension": self.embedding.dimension,
            },
            "last_built_at": datetime.now().isoformat(timespec="seconds"),
            "chunks_in_input": total_chunks,
            "chunks_extracted_this_run": outcome.chunks_processed,
            "chunks_cached_this_run": outcome.chunks_skipped_cached,
        }
        path = working_dir / self._MANIFEST_NAME
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Map ISO-639-1 / DocIngest language tags → LightRAG's prompt-template
# language names. LightRAG accepts free-form strings (it just substitutes
# {language} into prompt templates), so we use the canonical English names
# the LightRAG prompts were authored in.
_LANG_TAG_TO_NAME = {
    "ja": "Japanese",
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "zh-tw": "Chinese",
    "en": "English",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
}


def _resolve_language(language_cfg: str | None, output_dir: Path) -> str | None:
    """
    Resolve ``graph.lightrag.language`` config into the string LightRAG's
    prompt templates expect — or None when the user wants the default.

    Behaviour:
        - Empty / falsy / "default" → None (LightRAG keeps its English default).
        - "auto" → read index.json from output_dir, vote on the per-file
          ``language`` tally, map the winner via _LANG_TAG_TO_NAME.
          Falls back to None when index.json is missing or unanimous winner
          can't be determined; the LLM extractor's defaults still work,
          just not language-tuned.
        - Anything else → passed through verbatim, so callers can write
          "Japanese" / "ja" / "日本語" themselves and the value reaches
          LightRAG unchanged. (Translates known short tags as a courtesy.)
    """
    if not language_cfg:
        return None
    cfg = str(language_cfg).strip()
    if cfg.lower() in {"", "default"}:
        return None

    if cfg.lower() != "auto":
        # Translate short tags ("ja" → "Japanese") if we know them; pass
        # everything else through unchanged so users keep ultimate control.
        return _LANG_TAG_TO_NAME.get(cfg.lower(), cfg)

    # auto path: read index.json
    index_path = output_dir / "index.json"
    if not index_path.exists():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return None

    counts: Counter = Counter()
    for entry in files:
        if not isinstance(entry, dict):
            continue
        lang = str(entry.get("language", "") or "").strip().lower()
        if lang:
            counts[lang] += 1

    if not counts:
        return None
    winner_tag = counts.most_common(1)[0][0]
    return _LANG_TAG_TO_NAME.get(winner_tag, winner_tag.capitalize())


def _emit(
    callback: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    """Fire an on_progress callback, swallowing any exception."""
    if callback is None:
        return
    try:
        callback(event)
    except Exception as e:
        logger.warning(
            f"on_progress callback raised {type(e).__name__}: {e}; ignored."
        )


def _count_lightrag_artefacts(working_dir: Path) -> dict[str, int]:
    """
    Best-effort tally of LightRAG's on-disk artefacts. Used for build /
    status reporting — never raises; missing files just yield zero counts.

    LightRAG's default storage layout (NetworkX + NanoVectorDB) writes:
        - graph_chunk_entity_relation.graphml (the entity/relation graph)
        - vdb_entities.json, vdb_relationships.json, vdb_chunks.json
        - kv_store_*.json
    Counts are derived by parsing these files cheaply (no LightRAG import).
    """
    out = {"entities": 0, "relations": 0, "communities": 0}

    # entities & relations from the vector-db json files (one record per
    # vector — straightforward count).
    for fname, key in (
        ("vdb_entities.json", "entities"),
        ("vdb_relationships.json", "relations"),
    ):
        path = working_dir / fname
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            out[key] = len(data["data"])
        elif isinstance(data, list):
            out[key] = len(data)

    # communities — LightRAG stores them in kv_store_community_reports.json
    # (dict keyed by community id). Format may evolve; count keys when it's
    # a dict, fall back to 0 otherwise.
    cr_path = working_dir / "kv_store_community_reports.json"
    if cr_path.exists():
        try:
            data = json.loads(cr_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            out["communities"] = len(data)

    return out


def status_from_dir(
    output_dir: Path,
    output_subdir: str = "graph",
) -> StatusOutcome:
    """
    Directory-driven status check, used by ``api.status``.

    Doesn't construct a backend instance — purely reads the manifest +
    counts artefacts. Cheap, no LLM calls, no embedding work.
    """
    working_dir = output_dir / output_subdir
    if not working_dir.exists():
        return StatusOutcome(built=False, backend=LightRAGBackend.BACKEND_NAME)

    manifest_path = working_dir / LightRAGBackend._MANIFEST_NAME
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}

    counts = _count_lightrag_artefacts(working_dir)

    embedding_meta = manifest.get("embedding", {}) or {}

    return StatusOutcome(
        built=bool(counts["entities"] or counts["relations"] or manifest),
        backend=str(manifest.get("backend", LightRAGBackend.BACKEND_NAME)),
        mode=str(manifest.get("mode", "")),
        entities_count=counts["entities"],
        relations_count=counts["relations"],
        communities_count=counts["communities"],
        last_built_at=manifest.get("last_built_at"),
        embedding_model=str(embedding_meta.get("model", "")),
        embedding_dimension=int(embedding_meta.get("dimension", 0) or 0),
        extras={"working_dir": str(working_dir)},
    )


__all__ = [
    "LightRAGBackend",
    "status_from_dir",
]
