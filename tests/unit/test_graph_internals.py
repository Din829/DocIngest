"""
Tests for graph layer internals that don't require lightrag-hku to be
installed. Covers:

* chunks_loader: streaming, format filter, min_token filter, title-path
  enrichment, malformed-line tolerance.
* cache: chunk content hash, llm config hash, state save/load round-trip,
  is_chunk_cached decision logic.
* api facade: config builder, override normalization, embedding resolver
  (without instantiating the actual SDK clients — we only check the
  factory dispatch).

This file is safe to run on a bare core install (`pip install -e .`); it
must NOT import lightrag, openai, or sentence-transformers at the module
level.

Run:
    python tests/unit/test_graph_internals.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


# ---------------------------------------------------------------------------
# chunks_loader
# ---------------------------------------------------------------------------

def test_chunks_loader_streaming_and_filters() -> None:
    """Build a synthetic chunks.jsonl, then verify the loader respects all filters."""
    from docingest.graph.adapters.chunks_loader import iter_chunks, load_all

    records = [
        # short PDF chunk — gets filtered by min_chunk_tokens
        {"id": "a_chunk_000", "text": "hi", "metadata": {"format": "pdf", "title_path": "intro"}},
        # PDF chunk with usable length — keeps + enriched header
        {
            "id": "a_chunk_001",
            "text": (
                "This is a longer chunk that should pass the minimum token "
                "filter when min_chunk_tokens is set to a small number like 5."
            ) * 3,
            "metadata": {
                "format": "pdf",
                "title_path": "Section 1 > Intro",
                "original_file": "/some/path/report.pdf",
            },
        },
        # docx chunk — filtered out when formats=["pdf"]
        {
            "id": "b_chunk_000",
            "text": (
                "Long DOCX chunk content " * 20
            ),
            "metadata": {"format": "docx", "title_path": "Meeting notes"},
        },
        # malformed JSON line — must be skipped silently
    ]

    with tempfile.TemporaryDirectory() as tmp:
        chunks_path = Path(tmp) / "chunks.jsonl"
        with open(chunks_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write("{not valid json\n")
            f.write("\n")  # blank line — also skipped

        # No filters → 3 valid records (malformed + blank dropped).
        loaded_all = load_all(chunks_path)
        assert len(loaded_all) == 3, f"expected 3, got {len(loaded_all)}"

        # min_chunk_tokens filter drops the 2-byte chunk.
        loaded = load_all(chunks_path, min_chunk_tokens=10)
        assert len(loaded) == 2

        # Format filter restricts to pdf.
        loaded_pdf = load_all(chunks_path, formats=["pdf"], min_chunk_tokens=10)
        assert len(loaded_pdf) == 1
        only = loaded_pdf[0]

        # Title path enrichment: header prepended exactly once.
        assert only.text.startswith("[")
        assert "Section 1 > Intro" in only.text
        assert only.original_text == records[1]["text"]

        # Streaming form yields the same records as load_all in the same order.
        streamed_ids = [c.chunk_id for c in iter_chunks(chunks_path, min_chunk_tokens=10)]
        assert streamed_ids == [c.chunk_id for c in loaded]

    print("OK: chunks_loader streaming + filters + enrichment")


def test_chunks_loader_idempotent_header() -> None:
    """If chunk text already contains the title-path header, don't double it."""
    from docingest.graph.adapters.chunks_loader import iter_chunks

    metadata = {"original_file": "x.pdf", "title_path": "Top"}
    header = "[x.pdf > Top]\n"
    body = "Real chunk content goes here, long enough to pass token filters." * 5

    record = {"id": "x_chunk_000", "text": header + body, "metadata": metadata}

    with tempfile.TemporaryDirectory() as tmp:
        chunks_path = Path(tmp) / "chunks.jsonl"
        chunks_path.write_text(
            json.dumps(record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        loaded = list(iter_chunks(chunks_path))
        assert len(loaded) == 1
        # The enriched text must equal the input (no second header attached).
        assert loaded[0].text.count("[x.pdf > Top]") == 1

    print("OK: title_path enrichment is idempotent")


def test_chunks_loader_skips_when_main_pipeline_already_injected() -> None:
    """
    Real-world regression: the main pipeline's path_injection hook
    prepends '[来源: sources/x.md > Section]' to every chunk by default.
    The graph layer must detect this and NOT add a second, slightly
    different header — that wastes LLM tokens during extraction.
    """
    from docingest.graph.adapters.chunks_loader import iter_chunks

    metadata = {
        "source": "sources/report.md",
        "original_file": "report.pdf",
        "title_path": "Chapter 1 > Intro",
    }
    # Simulate what the main pipeline writes: '[来源:' marker + body.
    pipeline_header = "[来源: sources/report.md > Chapter 1 > Intro]\n"
    body = "Actual chunk content. " * 30
    raw_text = pipeline_header + body

    record = {"id": "y_chunk_000", "text": raw_text, "metadata": metadata}

    with tempfile.TemporaryDirectory() as tmp:
        chunks_path = Path(tmp) / "chunks.jsonl"
        chunks_path.write_text(
            json.dumps(record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        loaded = list(iter_chunks(chunks_path))
        assert len(loaded) == 1
        text = loaded[0].text

        # The pipeline's '[来源:' header must still be there (we didn't strip).
        assert "[来源:" in text
        # CRITICAL: we must NOT have prepended our own '[report.pdf > ...]'
        # header on top — that's the regression this test guards against.
        assert "[report.pdf >" not in text
        # Original text length must be unchanged (no new bytes prepended).
        assert text == raw_text

    print("OK: chunks_loader respects main-pipeline path_injection")


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def test_cache_round_trip() -> None:
    from docingest.graph import cache as graph_cache

    config = {
        "graph": {
            "llm": {
                "primary": {"provider": "openai", "model": "gpt-5.4-mini"},
                "fallback": {"provider": "google", "model": "gemini-3-flash-preview"},
                "max_response_tokens": 8192,
            },
            "lightrag": {"entity_extract_max_gleaning": 1},
        }
    }
    llm_hash = graph_cache.llm_config_hash(config)
    assert isinstance(llm_hash, str) and len(llm_hash) == 16

    text = "Hello there. This is a chunk."
    ch_hash = graph_cache.chunk_content_hash(text)

    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = Path(tmp) / ".graph_cache"

        # Empty state on first read.
        entries = graph_cache.load_state(cache_dir)
        assert entries == {}

        # mark + save round-trips.
        graph_cache.mark_extracted(entries, "x_chunk_000", ch_hash, llm_hash)
        graph_cache.save_state(cache_dir, entries)
        reloaded = graph_cache.load_state(cache_dir)
        assert reloaded.keys() == {"x_chunk_000"}
        assert reloaded["x_chunk_000"]["content_hash"] == ch_hash
        assert reloaded["x_chunk_000"]["llm_config_hash"] == llm_hash

        # is_chunk_cached: matching pair → True.
        assert graph_cache.is_chunk_cached(reloaded, "x_chunk_000", ch_hash, llm_hash)

        # Mismatching content → False.
        assert not graph_cache.is_chunk_cached(
            reloaded, "x_chunk_000", "deadbeef" * 4, llm_hash
        )

        # Mismatching llm config → False.
        assert not graph_cache.is_chunk_cached(
            reloaded, "x_chunk_000", ch_hash, "0" * 16
        )

    print("OK: cache hash + round-trip + decision logic")


def test_llm_config_hash_changes_with_relevant_paths() -> None:
    """Changing a relevant config path must change the hash; irrelevant
    fields must not."""
    from docingest.graph import cache as graph_cache

    base = {
        "graph": {
            "llm": {
                "primary": {"provider": "openai", "model": "gpt-5.4-mini"},
            },
            "lightrag": {"entity_extract_max_gleaning": 1},
            "output_subdir": "graph",
        }
    }
    hash_a = graph_cache.llm_config_hash(base)

    # Same relevant settings → same hash.
    hash_a_repeat = graph_cache.llm_config_hash(base)
    assert hash_a == hash_a_repeat

    # Change a NON-relevant path (output_subdir) — hash must stay same.
    altered_irrelevant = json.loads(json.dumps(base))
    altered_irrelevant["graph"]["output_subdir"] = "renamed_dir"
    assert graph_cache.llm_config_hash(altered_irrelevant) == hash_a

    # Change a RELEVANT path (model) — hash must change.
    altered_relevant = json.loads(json.dumps(base))
    altered_relevant["graph"]["llm"]["primary"]["model"] = "gpt-4o-mini"
    assert graph_cache.llm_config_hash(altered_relevant) != hash_a

    print("OK: llm_config_hash sensitive only to relevant paths")


# ---------------------------------------------------------------------------
# api facade
# ---------------------------------------------------------------------------

def test_api_normalize_overrides() -> None:
    """Both nested and flat dot-path overrides must produce identical config."""
    from docingest.graph.api import _normalize_overrides

    flat = _normalize_overrides({"graph.mode": "vector_only", "graph.cache.enabled": False})
    nested = _normalize_overrides({"graph": {"mode": "vector_only", "cache": {"enabled": False}}})
    assert flat == nested

    # Mix of both forms in the same input.
    mixed = _normalize_overrides({
        "graph.mode": "vector_only",
        "graph": {"cache": {"enabled": False}},
    })
    assert mixed == nested

    print("OK: _normalize_overrides flat == nested == mixed")


def test_api_resolve_embedding_dispatch() -> None:
    """
    The factory must pick the right concrete EmbeddingProvider class for
    each provider tag — without instantiating SDK clients (i.e. without
    triggering openai / google / sentence_transformers imports beyond the
    Provider class definitions).
    """
    from docingest.graph.api import _resolve_embedding
    from docingest.graph.providers import (
        OpenAIEmbedding,
        GeminiEmbedding,
        SentenceTransformerEmbedding,
    )

    # OpenAI
    cfg = {"graph": {"embedding": {"provider": "openai", "model": "text-embedding-3-small", "dimension": 1536}}}
    p = _resolve_embedding(cfg, None)
    assert isinstance(p, OpenAIEmbedding)
    assert p.dimension == 1536

    # Gemini
    cfg = {"graph": {"embedding": {"provider": "google", "model": "text-embedding-004", "dimension": 768}}}
    p = _resolve_embedding(cfg, None)
    assert isinstance(p, GeminiEmbedding)
    assert p.model == "text-embedding-004"

    # sentence_transformers (alias)
    cfg = {"graph": {"embedding": {"provider": "local", "model": "BAAI/bge-small-en-v1.5", "dimension": 384}}}
    p = _resolve_embedding(cfg, None)
    assert isinstance(p, SentenceTransformerEmbedding)

    # Explicit Provider object overrides config.
    explicit = OpenAIEmbedding(api_key="test-key")
    p = _resolve_embedding({"graph": {"embedding": {"provider": "google"}}}, explicit)
    assert p is explicit  # passthrough: no rebuild

    # Unknown provider → ValueError with helpful message.
    try:
        _resolve_embedding({"graph": {"embedding": {"provider": "weird"}}}, None)
    except ValueError as e:
        assert "weird" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown provider")

    print("OK: _resolve_embedding dispatch + override + error path")


def test_query_mode_validation() -> None:
    """The build-mode → query-mode validator gates global/hybrid for vector_only."""
    # The validator lives in lightrag_backend; we only need the function,
    # not the LightRAG instance, so importing it directly is fine.
    from docingest.graph.backends.lightrag_backend import _validate_query_mode

    # full mode — every documented LightRAG mode passes.
    for m in ("naive", "local", "global", "hybrid", "mix"):
        assert _validate_query_mode("full", m) == m

    # vector_only — only naive / local pass.
    assert _validate_query_mode("vector_only", "naive") == "naive"
    assert _validate_query_mode("vector_only", "LOCAL") == "local"  # case-insensitive

    for blocked in ("global", "hybrid", "mix"):
        try:
            _validate_query_mode("vector_only", blocked)
        except ValueError as e:
            assert "vector_only" in str(e)
        else:
            raise AssertionError(f"vector_only must reject mode={blocked}")

    # Unknown mode in full builds → ValueError.
    try:
        _validate_query_mode("full", "nonsense")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown query mode must raise")

    print("OK: query mode validation matrix")


def test_query_propagates_backend_error_on_empty_answer() -> None:
    """
    Regression guard: when a backend returns an empty answer (the way
    LightRAG silently fails on its Lock-event-loop bug), the facade
    QueryResult MUST carry the error in ``stats["error"]`` so callers
    can detect the failure.

    We don't exercise the real LightRAG path (that would require
    network + a built graph); instead we monkeypatch the backend
    factory to return a stub backend that mimics the empty-answer
    failure mode the production backend now reports.
    """
    from docingest.graph import api as graph_api
    from docingest.graph.backends.base import GraphBackend, QueryOutcome

    class _StubBackend(GraphBackend):
        """Minimal backend; the only method query() exercises is query()."""

        def build(self, chunks_path, output_dir, *, force=False, on_progress=None):  # type: ignore[override]
            raise NotImplementedError

        def status(self):  # type: ignore[override]
            raise NotImplementedError

        def query(self, query, *, mode, top_k=None, return_context=False, extra=None):  # type: ignore[override]
            # Mimic the post-fix backend behaviour: empty answer triggers
            # an explicit error entry in stats. The facade must surface this.
            return QueryOutcome(
                answer="",
                mode_used=mode,
                elapsed_ms=42,
                stats={"error": "synthetic: backend returned empty"},
            )

    # Patch the factory used by the facade. tmp dir avoids touching a
    # real knowledge base — the stub backend never reads disk.
    original_factory = graph_api._create_backend
    graph_api._create_backend = lambda config, embedding: _StubBackend()

    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = graph_api.query(
                "anything",
                knowledge_dir=tmp,
                mode="local",
            )
        finally:
            graph_api._create_backend = original_factory

    assert result.answer == ""
    assert isinstance(result.stats, dict)
    err = result.stats.get("error")
    assert err and "synthetic" in err, (
        f"facade did not propagate backend stats.error; got stats={result.stats!r}"
    )

    print("OK: facade propagates backend stats.error on empty answer")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> None:
    test_chunks_loader_streaming_and_filters()
    test_chunks_loader_idempotent_header()
    test_chunks_loader_skips_when_main_pipeline_already_injected()
    test_cache_round_trip()
    test_llm_config_hash_changes_with_relevant_paths()
    test_api_normalize_overrides()
    test_api_resolve_embedding_dispatch()
    test_query_mode_validation()
    test_query_propagates_backend_error_on_empty_answer()
    print("\nAll graph-internals tests passed.")


if __name__ == "__main__":
    main()
