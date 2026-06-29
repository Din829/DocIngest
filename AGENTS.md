# AGENTS.md

DocIngest = document preprocessing engine. Any input (PDF / Office / HTML /
images / audio / video / ZIP / URLs) -> Markdown + chunks + index.
It prepares data; it does not do retrieval, embeddings, or answer generation.

## Command catalog

Same command catalog as `.claude/skills/docingest/SKILL.md`.
Per-flag detail: `docingest <cmd> --help`.

**CLI** (`docingest <cmd>`)

| Command | What it does | Key flags |
|---|---|---|
| `run` | Docs -> Markdown + chunks + index | `-o/--output`; `--purpose markdown\|rag\|agentic\|full`; `--outputs md,chunks,index,...`; `--no-chunks`; `--engine docling\|vision_only\|azure_di`; `--strategy auto\|heading\|recursive\|slide\|sheet\|timestamp\|whole`; `--max-pages N`; `--parallel N`; `--parallel-files N`; `--force`; `-y/--yes`; `--json`; `--verbose/-v`; `-c/--config` |
| `inspect` | Estimate size/pages/cost without parsing | `--json` |
| `refine` | Markdown -> human-readable copy | `--skill refine_default\|refine_faithful\|refine_html`; `-o` |
| `doctor` | Check env / deps / API keys | none |
| `visualize` | Draw parse bounding boxes onto page images | `--pages`; `--labels`; `--numbers` |
| `export` | Knowledge base chunks -> vector store (opt-in `[azure]`) | `--to azure-search`; `--endpoint`; `--index`; `--search-key`; `--embed-provider azure-openai\|openai`; `--embed-model`; `--embed-dim`; `--embed-endpoint`; `--vector-field` |
| `extract` | Docs -> strongly-typed records via a YAML template (`extracted/<template>.jsonl`) | `-t/--template`; `-o/--output`; `--input sources\|chunks`; `--parallel N`; `--json` |
| `skills list` | List the refine SKILLs `refine --skill` can use (name + summary) | `--json` |

**Graph** (`docingest graph <cmd>`, requires `[graph]`)

| Command | What it does |
|---|---|
| `build` | Build graph artefacts from `chunks.jsonl` |
| `query` | Query a built graph |
| `status` | Report graph status and counts |
| `enrich` | Replay graph entities into `chunks_enriched.jsonl` |

**MCP** (`python -m docingest.mcp_server`)

Tools: `inspect`, `run`, `refine`, `build_graph`, `query_graph`,
`graph_status`, `enrich_chunks`.

## Workflow

For unknown or large inputs: `inspect` -> review cost -> `run` -> read
`knowledge_search.SKILL.md` -> search `sources/*.md` or consume `chunks.jsonl`.

Optional second pass, after `run`: `extract` (template -> one strongly-typed
record per doc in `extracted/*.jsonl`) when the user wants a structured table,
not free text.

## Docs

- Embedding DocIngest into another system (decision frame + traps + recipes) -> [docs/INTEGRATION.md](docs/INTEGRATION.md)
- Architecture / phases / extension points -> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Where DocIngest sits vs. competitors, and dev priorities -> [docs/COMPETITIVE_POSITIONING.md](docs/COMPETITIVE_POSITIONING.md)
