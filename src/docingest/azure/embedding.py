"""
Embedding providers for the Azure AI Search export — generic, injectable.

Why a SEPARATE embedding hierarchy from docingest.graph.providers:
    graph's EmbeddingProvider is bound to LightRAG (its only exit is
    to_lightrag_func() returning a LightRAG object) and lives in the GraphRAG
    subpackage. The azure export feeds Azure AI Search, a different consumer.
    Coupling the two plugins (or depending on graph's private _async_embed)
    would break their independence. The small overlap in "call OpenAI, get
    vectors" is the deliberate price of keeping the two plugins decoupled —
    each owns its own thin embedding shim.

Contract — every provider exposes:
    embed(texts: list[str]) -> list[list[float]]   # one vector per text
    dimension: int                                  # vector length, for the
                                                    # index-dimension check

Add a provider = subclass SearchEmbeddingProvider + implement embed(). Callers
can also pass any object with that shape (duck-typed) — no inheritance required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SearchEmbeddingProvider:
    """Base: holds model/dim, subclasses implement embed().

    Fields:
        model     — concrete model / deployment identifier.
        api_key   — plaintext key; explicit value beats env at call time.
        dimension — vector length. MUST match the index's vector field
                    `dimensions`; search_export verifies this before pushing
                    and fails loud on mismatch (Azure rejects mismatched dims).
    """

    model: str
    api_key: str | None = None
    dimension: int = 1536

    def embed(self, _texts: list[str]) -> list[list[float]]:
        """Return one vector (list[float]) per input text, in order."""
        raise NotImplementedError


@dataclass
class OpenAIEmbedding(SearchEmbeddingProvider):
    """Public OpenAI embeddings via the official ``openai`` SDK.

    Default text-embedding-3-small (1536). For -3-large pass dimension=3072.
    text-embedding-3-* accept a `dimensions` arg to truncate; we pass it so the
    returned vector length always equals self.dimension (keeps the index check
    honest). ada-002 ignores it (fixed 1536) — harmless.
    """

    model: str = "text-embedding-3-small"
    api_key: str | None = None
    dimension: int = 1536

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "OpenAIEmbedding requires the openai SDK. "
                "Install with: pip install -e \".[azure]\"."
            ) from e

        key = self.api_key or os.environ.get("OPENAI_API_KEY")
        client = OpenAI(api_key=key) if key else OpenAI()
        # text-embedding-3-* support `dimensions`; ada-002 does not and errors
        # if it's sent, so only pass it for the 3-series.
        kwargs = {"model": self.model, "input": texts}
        if self.model.startswith("text-embedding-3"):
            kwargs["dimensions"] = self.dimension
        resp = client.embeddings.create(**kwargs)
        return [item.embedding for item in resp.data]


@dataclass
class AzureOpenAIEmbedding(SearchEmbeddingProvider):
    """Azure-hosted OpenAI embeddings via the ``openai`` SDK's AzureOpenAI client.

    Azure routes through a DEPLOYMENT, not a model id — ``model`` holds the
    deployment name (what you named it in the Azure portal). The underlying
    model (text-embedding-3-small/-large/ada-002) is whatever the deployment
    was provisioned with, so the caller must set ``dimension`` to match it.

    Required:
        endpoint:     https://<resource>.openai.azure.com/
        api_version:  e.g. "2024-02-01"
        api_key:      Azure OpenAI key (or set AZURE_OPENAI_API_KEY env)
    """

    model: str = ""                  # Azure deployment name (NOT a model id)
    api_key: str | None = None
    dimension: int = 1536
    endpoint: str | None = None
    api_version: str = "2024-02-01"

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            from openai import AzureOpenAI
        except ImportError as e:
            raise ImportError(
                "AzureOpenAIEmbedding requires the openai SDK. "
                "Install with: pip install -e \".[azure]\"."
            ) from e

        endpoint = self.endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        key = self.api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        missing = [
            n for n, v in (("endpoint", endpoint), ("api_key", key), ("model/deployment", self.model))
            if not v
        ]
        if missing:
            raise ValueError(
                f"AzureOpenAIEmbedding missing {missing}. Provide endpoint / "
                f"api_key / deployment (model=) explicitly or via "
                f"AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY env."
            )

        client = AzureOpenAI(
            api_key=key, azure_endpoint=endpoint, api_version=self.api_version
        )
        # On Azure, `model` is the deployment name. The 3-series `dimensions`
        # arg works the same as public OpenAI.
        kwargs = {"model": self.model, "input": texts}
        resp = client.embeddings.create(**kwargs)
        return [item.embedding for item in resp.data]


__all__ = [
    "SearchEmbeddingProvider",
    "OpenAIEmbedding",
    "AzureOpenAIEmbedding",
]
