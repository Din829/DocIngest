---
name: docingest
description: >-
  Document preprocessing engine — turn any input (PDF/Office/HTML/images/audio/video/ZIP/URLs)
  into clean Markdown + chunks + a searchable index for RAG and Agentic Search. Use when the
  user wants to ingest, parse, or extract documents into a knowledge base, estimate processing
  cost, or build a knowledge graph. Three channels: CLI (`docingest run/inspect/refine/doctor`
  and `docingest graph build/query/status/enrich`), MCP tools, and the Python library
  (`import docingest`). NOT a retrieval engine — it prepares data; you search it with your own
  Grep/Read on `sources/*.md` or vector search on `chunks.jsonl`.
---

# DocIngest — command catalog

DocIngest converts any document into clean Markdown (`sources/*.md`), chunked text
(`chunks.jsonl`), a file index (`index.json`), and an auto-generated search guide
(`knowledge_search.SKILL.md`). It does **no** embeddings, **no** vector search, **no**
answer generation — you retrieve over its output with your own tools.

Same capability is reachable three ways. This table is the single source of truth;
pick the channel that matches how you're calling DocIngest.

## CLI commands (`docingest <cmd>`)

| Command | What it does | When to use / skip | Key flags |
|---|---|---|---|
| `run` | Process docs → Markdown + chunks + index | Main command. Run after `inspect` for large/unknown inputs; directly for small trusted ones. Re-running the same `-o` is cheap (incremental cache). | `-o/--output` (REQUIRED for multi-input); `--no-chunks`; `--strategy auto\|heading\|recursive\|slide\|sheet`; `--parallel N` (Vision/ASR workers); `--force`; `-y/--yes`; `--json`; `--verbose/-v`; `-c/--config` |
| `inspect` | Estimate size/pages/cost — **no parsing** | ALWAYS first for large/unknown/expensive inputs (Vision is 1 API call per page). Skip only for a few small text files. | `--json`; `-c/--config` |
| `refine` | Rewrite a Markdown file into a human-readable version | ONLY when the user wants a readable/published version. **NOT** a RAG step — RAG consumes the raw `sources/*.md`. | `--skill refine_default\|refine_faithful\|refine_html`; `-o/--output`; `-c/--config` |
| `doctor` | Check environment: packages, external tools, API keys | After install, or when something fails unexpectedly. | (none) |

## graph subcommands (`docingest graph <cmd>`) — opt-in

Requires `pip install -e ".[graph]"`. **More expensive than Vision** — only for
"themes / relationships / multi-hop across the corpus" questions; for single-fact
lookup your native Grep on `sources/*.md` is cheaper.

| Command | What it does | When to use | Key flags |
|---|---|---|---|
| `build` | Build a knowledge graph on top of `chunks.jsonl` | After a successful `run`. First build of a large corpus costs real money — surface the estimate first. | `--mode vector_only\|full`; `--force`; `--enrich-chunks`; `--json` |
| `query` | Ask the graph a question | After `build`. Check `status` first if unsure it's built. | `--kb`; `--mode naive\|local\|global\|hybrid\|mix`; `--top-k`; `--json` |
| `status` | Show whether a graph is built + counts | Before `query`. | `--json` |
| `enrich` | Replay graph entities into `chunks_enriched.jsonl` | Want traditional vector RAG to also benefit from the graph. No LLM calls — pure replay. | `--json` |

> `build --enrich-chunks` and standalone `enrich` do the same thing. Use the flag
> during build; use standalone `enrich` when the graph was built earlier without it.

## MCP tools (when running as an MCP server)

`python -m docingest.mcp_server` exposes 7 tools (same behaviour as the CLI/Python API):

`inspect` · `run` · `refine` · `build_graph` · `query_graph` · `graph_status` · `enrich_chunks`

The 4 `*graph*` tools appear only when `[graph]` is installed. Note: `graph_status`
takes only `knowledge_dir` (no `config_overrides`).

Searching the knowledge base is **not** an MCP tool by design — read
`<output>/knowledge_search.SKILL.md` first (auto-generated search protocol), then use
your own Grep/Read on `sources/*.md`.

## Conventions shared by ALL commands

- **Streams**: JSON → stdout; banner / progress / errors → stderr. Safe for piping.
- **Exit codes**: `0` success · `1` failures · `2` safety abort (re-run with `--yes` after reviewing).
- **Incremental**: `run` skips unchanged files by content hash. Don't pass `--force` "to be safe" — it's expensive.
- **Safety**: by default (`safety.mode=strict`) over-budget runs abort. Surface the cost to the user, then proceed with `--yes` (CLI) / `acknowledge_large=True` (Python/MCP).
- **Fine-grained output control**: to produce only some artefacts (and skip their LLM cost), the Python API has `outputs=["markdown","chunks",...]`; CLI/MCP expose only the coarse `--no-chunks` / `no_chunks`, otherwise pass `config_overrides` to disable stages (`knowledge_map.enabled`, etc.).
- **Credentials**: inject via Provider classes (`docingest.GeminiProvider(api_key=...)`, also Azure/Bedrock/Vertex/OpenAI/Anthropic/DashScope), env vars, or `config_overrides` — pick one.

## Typical agent workflow

1. `inspect(paths)` → review `est_cost_usd` + `recommendation`
2. `run(paths, -o <dir>)` → process (incremental; second run is seconds)
3. Read `<dir>/knowledge_search.SKILL.md` + `<dir>/index.json` → orient
4. Grep/Read `<dir>/sources/*.md` → find & read (expand synonyms; "Grep miss ≠ not written")
5. (optional) `refine(...)` for a human-readable copy

Full reference: [README.md](../../../README.md) · per-flag help: `docingest <cmd> --help`.
