"""
Optional helper: create an Azure AI Search index with a vector field.

Standalone convenience for users who don't already have an index. Builds the
official recommended vector schema (HNSW algorithm + a vector-search profile +
a Collection(Edm.Single) vector field) so newcomers don't have to assemble the
VectorSearch config by hand. push_to_azure_search does NOT call this — it
assumes the index exists; this is for first-time setup only.

Field names mirror search_export.DEFAULT_FIELD_MAP so an index created here and
a default export line up out of the box; override to match an existing schema.
"""

from __future__ import annotations

import logging

from .search_export import DEFAULT_FIELD_MAP

logger = logging.getLogger(__name__)


def create_azure_search_index(
    *,
    search_endpoint: str,
    index_name: str,
    dimension: int,
    search_key: str | None = None,
    field_map: dict[str, str] | None = None,
    metric: str = "cosine",
) -> None:
    """Create (or update) a vector-search index with a standard schema.

    Args:
        dimension: vector field dimension — MUST equal your embedding model's
            output dimension (e.g. 1536 for text-embedding-3-small, 3072 for
            -large). The export's dimension check keys off this.
        metric: similarity metric. "cosine" for Azure OpenAI embeddings
            (their recommendation); also "dotProduct" / "euclidean".
        field_map: override field names (same keys as DEFAULT_FIELD_MAP).

    Schema produced (logical → index field):
        id      → key, Edm.String, filterable
        content → Edm.String, searchable, retrievable
        vector  → Collection(Edm.Single), dimensions=<dimension>, HNSW profile
        source / title_path / format → Edm.String, filterable, retrievable
    """
    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents.indexes import SearchIndexClient
        from azure.search.documents.indexes.models import (
            SearchIndex,
            SearchField,
            SimpleField,
            SearchableField,
            SearchFieldDataType,
            VectorSearch,
            VectorSearchProfile,
            HnswAlgorithmConfiguration,
        )
    except ImportError as e:
        raise ImportError(
            "create_azure_search_index requires azure-search-documents. "
            "Install with: pip install -e \".[azure]\"."
        ) from e

    fmap = {**DEFAULT_FIELD_MAP, **(field_map or {})}

    import os
    key = search_key or os.environ.get("AZURE_SEARCH_API_KEY")
    if key:
        client = SearchIndexClient(search_endpoint, AzureKeyCredential(key))
    else:
        from azure.identity import DefaultAzureCredential
        client = SearchIndexClient(search_endpoint, DefaultAzureCredential())

    algo_name = "hnsw-config"
    profile_name = "vector-profile"

    fields = [
        SimpleField(name=fmap["id"], type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name=fmap["content"], type=SearchFieldDataType.String, retrievable=True),
        SearchField(
            name=fmap["vector"],
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=dimension,
            vector_search_profile_name=profile_name,
        ),
        SimpleField(name=fmap["source"], type=SearchFieldDataType.String, filterable=True, retrievable=True),
        SimpleField(name=fmap["title_path"], type=SearchFieldDataType.String, filterable=True, retrievable=True),
        SimpleField(name=fmap["format"], type=SearchFieldDataType.String, filterable=True, retrievable=True),
    ]

    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name=algo_name)],
        profiles=[VectorSearchProfile(name=profile_name, algorithm_configuration_name=algo_name)],
    )

    index = SearchIndex(name=index_name, fields=fields, vector_search=vector_search)
    client.create_or_update_index(index)
    logger.info("Created/updated Azure Search index '%s' (%d-dim vector field '%s')",
                index_name, dimension, fmap["vector"])


__all__ = ["create_azure_search_index"]
