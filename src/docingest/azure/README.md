# docingest.azure — Azure Document Intelligence parsing backend (plugin)

Opt-in plugin that replaces DocIngest's **parse step** (Phase 1) with Azure
Document Intelligence's cloud service. Everything downstream — write / chunk /
index / knowledge_map / graph — is unchanged, because this backend produces the
same `ParseResult` contract as the default docling parser.

## Why

docling's PDF backend (`docling-parse`) has a Windows `std::bad_alloc`
regression that DocIngest works around with batched parsing
(see `docs/docling_parse_OOM_Windows_长期监控.md`). Azure DI **sidesteps the bug
at the source**: the parse runs in Azure's cloud, so the local machine only
uploads bytes and polls for a result — no local C++ memory pressure, no OOM.

> Not the same as `AzureOpenAIProvider` in `docingest.providers`. That injects
> an Azure-hosted GPT model for the **Vision LLM** step. Azure DI is a document
> **parsing** service — a different product, a different pipeline phase.

## Install

```bash
pip install -e ".[azure]"     # pulls azure-ai-documentintelligence
```

## Use

Pick the engine + give credentials. Three credential paths (highest wins):
provider object → config → env.

```python
import docingest
from docingest.azure import DocIntelligenceProvider

# Via provider (no env / YAML needed)
docingest.ingest(
    "./docs/", output="./kb/",
    config_overrides={
        "parsing.engine": "azure_di",
        "parsing.azure_di": DocIntelligenceProvider(
            endpoint="https://<resource>.cognitiveservices.azure.com/",
            api_key="...",
        ).to_config(),                      # or set env / YAML, see below
    },
)
```

```bash
# Via env + CLI
export AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT="https://<resource>.cognitiveservices.azure.com/"
export AZURE_DOCUMENT_INTELLIGENCE_KEY="..."
docingest run ./docs/ -o ./kb/ -c <a config with parsing.engine: azure_di>
```

Or in a project `docingest.yaml`:

```yaml
parsing:
  engine: azure_di
  azure_di:
    endpoint: https://<resource>.cognitiveservices.azure.com/
    # api_key: prefer env over committing a key
```

## Export to Azure AI Search (output side)

Independent of the parser above. Push a knowledge base's chunks into an Azure AI
Search vector index using your OWN embedding ("manual vectorization" — the path
Azure documents for teams who keep their own chunking + embedding). DocIngest's
high-quality chunks are preserved; Azure's integrated vectorization would
re-chunk and waste them, which is exactly why this path exists.

Generic by design — nothing is hardwired:
- **field names** come from `field_map` (override per index)
- **metadata channel is open-ended** — `field_map` has three core keys
  (`id` / `content` / `vector`); EVERY other key is a metadata mapping
  (`logical chunk.metadata key → your index field`). Want to push `language` or
  `author`? Just add it: `field_map={"language": "lang", "author": "by"}`. No
  code change, no fixed allow-list.
- **embedding** is injected (`AzureOpenAIEmbedding` / `OpenAIEmbedding` / your own
  object with `.embed()` + `.dimension`)
- the **vector dimension** is verified against the index before any upload (fail
  loud on mismatch — Azure rejects mismatched dims)

The full Azure loop (free local parse → Azure Search), two steps:

```bash
# 1. Parse locally with docling (free, run it overnight) → chunks.jsonl
docingest run ./docs/ -o ./kb/

# 2. Embed + push into your Azure AI Search index (one command, no glue code)
docingest export ./kb/ --to azure-search \
    --endpoint https://<svc>.search.windows.net --index my-index \
    --search-key <admin-key> \
    --embed-provider azure-openai --embed-model my-embed-deployment \
    --embed-dim 1536 --embed-endpoint https://<res>.openai.azure.com/ \
    --vector-field content_vector        # match your index's field name
```

Library form (embedding fully injectable; bring your own index):

```python
from docingest.azure import push_to_azure_search, AzureOpenAIEmbedding

push_to_azure_search(
    "./kb/",
    search_endpoint="https://<svc>.search.windows.net",
    index_name="my-index",
    search_key="<admin-key>",
    embedding=AzureOpenAIEmbedding(
        model="my-embed-deployment",
        endpoint="https://<res>.openai.azure.com/",
        api_key="...", dimension=1536,
    ),
    field_map={"vector": "content_vector"},   # only override what differs
)
```

Don't have an index yet? Make a standard one (HNSW + vector field) first:

```python
from docingest.azure import create_azure_search_index
create_azure_search_index(
    search_endpoint=..., index_name="my-index", dimension=1536, search_key=...,
)
```

## Layout of this plugin

| File | Job |
|---|---|
| `di_parser.py` | `AzureDIParser(BaseParser)` — parse: the network call (upload + poll), credential resolution, error isolation |
| `converter.py` | Pure functions: DI `AnalyzeResult` → markdown + `PageData` + metadata. SDK-free, unit-testable |
| `provider.py` | `DocIntelligenceProvider` credential dataclass (parse) |
| `embedding.py` | export: `SearchEmbeddingProvider` base + `OpenAIEmbedding` / `AzureOpenAIEmbedding` |
| `search_export.py` | export: chunks.jsonl → embed → `upload_documents`, with dimension check + field mapping |
| `search_index.py` | export: optional `create_azure_search_index` helper (standard vector schema) |
| `__init__.py` | Opt-in package marker — does NOT import any azure SDK at top level |

## How it plugs in (one branch)

`parsers/__init__.py` → `create_parser()` has a single `engine == "azure_di"`
branch that imports `AzureDIParser`. That is the **only** core-pipeline touch
point.

## Remove it cleanly

1. Delete this directory (`src/docingest/azure/`).
2. Delete the `azure_di` branch in `parsers/__init__.py`.
3. (Optional) drop the `[azure]` extra in `pyproject.toml`, the
   `parsing.azure_di` block in `config/default.yaml`, and the
   `parsing.azure_di.model_id` line in `incremental.py`.

DocIngest reverts to docling exactly — nothing in the core pipeline depends on
this plugin.

## Known limits / TODO (not yet validated against a live endpoint)

The converter was written against the official SDK docs, not a live call.
Two things must be confirmed with a real `AnalyzeResult` before relying on it:

- **Tables are HTML** in DI v4.0 markdown output (`<table>`, for merged cells /
  multi-row headers), not pipe tables. `converter` keeps them verbatim. Verify
  the chunker's protected-block handling treats an HTML table block sensibly
  (it may need an HTML-table protector, or a HTML→pipe conversion step here).
- **Page splitting** uses DI's per-page `spans` (offset/length into the
  ORIGINAL `content`). `converter` deliberately slices pages from the raw
  content, then strips furniture only for the body markdown — so furniture
  removal never shifts the offsets used for page slicing. Confirm on a real
  multi-page result that each page's sliced text matches what you'd expect.

See `tests/unit/test_azure_di.py` for the offline tests that already pass.
