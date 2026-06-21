"""
docingest.azure — optional Azure Document Intelligence parsing backend.

A self-contained PLUGIN: select it with ``parsing.engine: azure_di`` and the
whole parse step runs in Azure's cloud instead of local docling. Everything
downstream (write / chunk / index / knowledge_map / graph) is unchanged — the
backend produces the same ParseResult contract.

Why a plugin and not a patch to the existing parsers:
  - It sidesteps the docling-parse Windows std::bad_alloc bug at the source
    (parse happens in the cloud — no local C++ memory pressure).
  - It is opt-in and removable: delete this directory + the one ``azure_di``
    branch in parsers/__init__.py and DocIngest reverts to docling exactly.
    All Azure-DI code lives here, so review / extend / drop is one directory.

Optional dependency policy (same as docingest.graph):
  Importing ``docingest.azure`` does NOT import the azure SDK — the import is
  deferred into AzureDIParser._get_client(), so ``docingest doctor`` and tests
  work without the ``[azure]`` extra installed. Only an actual azure_di parse
  needs ``azure-ai-documentintelligence``.

Note: this is distinct from ``AzureOpenAIProvider`` in ``docingest.providers``
— that injects an Azure-hosted GPT model for the Vision LLM step. Azure DI is a
document PARSING service, a different product and a different pipeline phase.

Two independent, opt-in capabilities live here (use either, both, or neither):

  PARSING (input side)   — Azure DI as a parse backend, selected with
                           parsing.engine=azure_di. Sidesteps the docling-parse
                           Windows OOM bug (parse in the cloud).
  EXPORT (output side)   — push DocIngest chunks → Azure AI Search with your own
                           embedding (manual vectorization). Generic: field names
                           and embedding are injectable, nothing is hardwired.

Both are deferred-import: importing docingest.azure pulls NO azure SDK; each SDK
loads only when its capability actually runs.

Public surface:
    # parsing
    AzureDIParser            — BaseParser backend (also via parsing.engine=azure_di)
    DocIntelligenceProvider  — DI credential injection dataclass
    convert                  — DI AnalyzeResult → ParseResult pieces (pure)
    # export
    push_to_azure_search     — chunks.jsonl → embed → Azure AI Search
    ExportResult             — push outcome (uploaded / failed / errors)
    create_azure_search_index — optional helper: make a standard vector index
    SearchEmbeddingProvider  — embedding base (subclass / duck-type to extend)
    OpenAIEmbedding, AzureOpenAIEmbedding — ready embedding providers
"""

from __future__ import annotations

from .di_parser import AzureDIParser
from .provider import DocIntelligenceProvider
from .converter import convert
from .search_export import push_to_azure_search, ExportResult
from .search_index import create_azure_search_index
from .embedding import (
    SearchEmbeddingProvider,
    OpenAIEmbedding,
    AzureOpenAIEmbedding,
)

__all__ = [
    # parsing
    "AzureDIParser",
    "DocIntelligenceProvider",
    "convert",
    # export
    "push_to_azure_search",
    "ExportResult",
    "create_azure_search_index",
    "SearchEmbeddingProvider",
    "OpenAIEmbedding",
    "AzureOpenAIEmbedding",
]
