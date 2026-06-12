"""
Embedding provider classes for the GraphRAG layer.

Why a NEW provider hierarchy (instead of extending docingest.providers):
    The main DocIngest pipeline never embeds — it only does parse / Vision /
    ASR / chunk. Embedding is exclusive to the optional graph layer, so we
    keep its provider classes here rather than polluting the stable public
    surface in docingest.providers.

Shape mirrors docingest.providers (dataclass + .to_lightrag_func() instead
of .to_model_config()) so callers familiar with the main facade hit no
surprise. Each provider is dumb: holds credentials + model name + dimension,
and exposes a builder that returns the async function shape LightRAG
expects (``EmbeddingFunc(embedding_dim=..., max_token_size=..., func=...)``).

Adding a new embedding provider = subclass + override ``_async_embed``. The
LightRAG-facing wrapper logic (batching, EmbeddingFunc construction) lives
once in the base class.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

# numpy is a transitive dependency of litellm/docling — already in core install.
import numpy as np


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

@dataclass
class EmbeddingProvider:
    """
    Base for embedding providers. Subclasses fill in ``_async_embed`` to call
    the actual provider SDK.

    Fields:
        provider     — provider tag matching litellm conventions
                       ("openai" / "google" / "sentence_transformers" / ...).
        model        — concrete model identifier.
        api_key      — plaintext key. When set, written to the relevant env
                       var at call time (matches docingest.providers).
        dimension    — embedding vector dimension. MUST match the model;
                       LightRAG bakes this into vector storage at build time
                       and rejects mismatched dims at query time.
        max_token_size — hard cap on input tokens per text. Texts longer than
                       this should be truncated by the caller; the provider
                       never silently truncates.
    """

    provider: str
    model: str
    api_key: str | None = None
    dimension: int = 1536
    max_token_size: int = 8192

    async def _async_embed(self, _texts: list[str]) -> np.ndarray:
        """Subclasses override. Must return shape (len(texts), dimension)."""
        raise NotImplementedError

    def to_lightrag_func(self) -> "Any":
        """
        Build a LightRAG ``EmbeddingFunc`` wrapper for this provider.

        Returns the actual ``lightrag.utils.EmbeddingFunc`` instance LightRAG
        wants — imported lazily because the graph subpackage must remain
        importable without lightrag installed (e.g. for ``docingest doctor``
        or for tests that mock the backend).
        """
        from lightrag.utils import EmbeddingFunc

        async def _func(texts: list[str]) -> np.ndarray:
            return await self._async_embed(texts)

        return EmbeddingFunc(
            embedding_dim=self.dimension,
            max_token_size=self.max_token_size,
            func=_func,
        )


# ---------------------------------------------------------------------------
# OpenAI (text-embedding-3-small / -large / ada-002)
# ---------------------------------------------------------------------------

@dataclass
class OpenAIEmbedding(EmbeddingProvider):
    """
    OpenAI embeddings via the official ``openai`` SDK.

    Default model is text-embedding-3-small (1536 dim) — matches
    config/default.yaml. To use text-embedding-3-large pass dimension=3072.
    """

    provider: str = "openai"
    model: str = "text-embedding-3-small"
    api_key: str | None = None
    dimension: int = 1536
    max_token_size: int = 8192

    async def _async_embed(self, texts: list[str]) -> np.ndarray:
        # Lazy import: the openai SDK is a transitive dep of litellm but
        # importing it at module load would surprise users running doctor /
        # tests in environments that intentionally don't have it.
        from openai import AsyncOpenAI

        # api_key precedence: explicit > env. Mirrors docingest.providers.
        key = self.api_key or os.environ.get("OPENAI_API_KEY")
        client = AsyncOpenAI(api_key=key) if key else AsyncOpenAI()

        resp = await client.embeddings.create(
            model=self.model,
            input=texts,
        )

        # Embedding tokens are a real (if small) spend the run report
        # otherwise misses — record verbatim from the response (OpenAI's
        # embeddings API reliably reports usage; if it ever doesn't, the
        # call itself is still an exact fact worth a ledger line).
        from ..models.token_tracker import token_tracker
        usage = getattr(resp, "usage", None)
        token_tracker.record(
            model=f"openai/{self.model}",
            prompt=getattr(usage, "prompt_tokens", 0) or 0,
            total_reported=getattr(usage, "total_tokens", 0) or 0,
        )

        return np.array([item.embedding for item in resp.data], dtype=np.float32)


# ---------------------------------------------------------------------------
# Google (Gemini gemini-embedding-001)
# ---------------------------------------------------------------------------

@dataclass
class GeminiEmbedding(EmbeddingProvider):
    """
    Google Gemini embeddings via the official ``google-genai`` SDK.

    Default model is gemini-embedding-001 (Matryoshka, supports 768 / 1536 /
    3072 dim). We default to 768 to preserve the dimension contract that
    older LightRAG indices were built against — the SDK is asked to truncate
    to ``self.dimension`` via ``EmbedContentConfig.output_dimensionality``.

    The dimension MUST be passed explicitly: the API's own default is 3072,
    so accepting the API default would silently break any existing vector
    store dimensioned at 768.

    Migration note: replaces the deprecated ``google-generativeai`` SDK
    (EOL 2025-11-30). The new SDK's model names do NOT take the ``models/``
    prefix the old SDK required — we strip it if present so configs written
    for the old SDK still work.
    """

    provider: str = "google"
    model: str = "gemini-embedding-001"
    api_key: str | None = None
    dimension: int = 768
    max_token_size: int = 2048

    async def _async_embed(self, texts: list[str]) -> np.ndarray:
        # Lazy import: google-genai is NOT a transitive dependency of the
        # docingest core; users opting into Gemini embeddings install it
        # themselves (see [graph-gemini] extras).
        from google import genai  # type: ignore[import-not-found]
        from google.genai import types  # type: ignore[import-not-found]

        key = self.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        # google-genai falls back to ambient credentials (ADC, Vertex) when
        # api_key is None — mirror that by passing api_key only when set.
        client = genai.Client(api_key=key) if key else genai.Client()

        # Strip legacy "models/" prefix — required by old SDK, forbidden by new.
        model_name = self.model.removeprefix("models/")

        # One batched call — new SDK accepts `contents` as a list, no need for
        # the per-text asyncio.gather + to_thread dance the old sync SDK forced.
        # `contents=list[str]` is documented in the SDK's README; the type stub
        # spelling (ContentListUnion) is wider than that, hence the ignore.
        response = await client.aio.models.embed_content(
            model=model_name,
            contents=texts,  # type: ignore[arg-type]
            config=types.EmbedContentConfig(output_dimensionality=self.dimension),
        )
        embeddings = response.embeddings
        if not embeddings:
            # API contract violation — texts went in but no embeddings came out.
            # Fail loud rather than return a misshapen array LightRAG can't use.
            raise RuntimeError(
                f"google-genai embed_content returned no embeddings for "
                f"{len(texts)} input(s); response={response!r}"
            )

        # Probed live (2026-06-12): the google-genai SDK's
        # EmbedContentResponse carries NO usage fields on the API-key
        # channel — top level is just embeddings/metadata, and both
        # metadata and per-embedding statistics come back None (the REST
        # docs' usageMetadata is only populated for Vertex callers). With
        # no reported figure there is nothing accurate to record, so we
        # record the CALL itself (an exact fact) with zero tokens — the
        # ledger shows "N embedding calls, unmetered" instead of nothing.
        from ..models.token_tracker import token_tracker
        token_tracker.record(model=f"gemini/{model_name}")

        return np.array([emb.values for emb in embeddings], dtype=np.float32)


# ---------------------------------------------------------------------------
# Local sentence-transformers (zero API cost)
# ---------------------------------------------------------------------------

# Module-level model cache shared across all SentenceTransformerEmbedding
# instances. Keyed by model name so different models coexist; the same
# model loaded by multiple providers reuses one in-memory copy.
_ST_MODEL_CACHE: dict[str, Any] = {}


@dataclass
class SentenceTransformerEmbedding(EmbeddingProvider):
    """
    Local sentence-transformers embedding (no API calls, no cost).

    Defaults to BGE-small-en (384 dim, ~130MB download on first use). For
    multilingual / Chinese / Japanese workloads pass model="BAAI/bge-m3"
    (1024 dim) or "intfloat/multilingual-e5-large" (1024 dim).

    Requires ``pip install -e ".[graph-local]"``. Importing this class is
    safe; using it without the package installed raises a clear ImportError
    on first call.

    Model loading is cached per process — the first ``_async_embed`` call
    loads the model, subsequent calls reuse it. Loading happens on a worker
    thread so it doesn't block the event loop.
    """

    provider: str = "sentence_transformers"
    model: str = "BAAI/bge-small-en-v1.5"
    api_key: str | None = None              # unused for local models
    dimension: int = 384
    max_token_size: int = 512

    def _get_model(self):
        if self.model not in _ST_MODEL_CACHE:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
            _ST_MODEL_CACHE[self.model] = SentenceTransformer(self.model)
        return _ST_MODEL_CACHE[self.model]

    async def _async_embed(self, texts: list[str]) -> np.ndarray:
        def _encode() -> np.ndarray:
            model = self._get_model()
            # convert_to_numpy=True yields float32 by default — matches the
            # OpenAI/Gemini paths above for downstream comparability.
            return model.encode(texts, convert_to_numpy=True, show_progress_bar=False)

        return await asyncio.to_thread(_encode)


__all__ = [
    "EmbeddingProvider",
    "OpenAIEmbedding",
    "GeminiEmbedding",
    "SentenceTransformerEmbedding",
]
