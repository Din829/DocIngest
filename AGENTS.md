# AGENTS.md

DocIngest = document preprocessing engine. Any input (PDF / Office / HTML /
images / audio / video / ZIP / URLs) → Markdown + chunks + index.
**No retrieval, no embeddings, no answer generation** — it prepares data; you
search it with your own Grep/Read on `sources/*.md` or vector search on `chunks.jsonl`.

## Command catalog — read this before you start

Same table as `.claude/skills/docingest/SKILL.md` (single source of truth).
Per-flag detail: `docingest <cmd> --help`.

**CLI** (`docingest <cmd>`)

| Command | What it does | When | Key flags |
|---|---|---|---|
| `run` | Docs → Markdown + chunks + index | Main command; after `inspect` for big inputs. Re-running same `-o` is cheap. | `-o` (REQUIRED for multi-input); `--no-chunks`; `--strategy auto\|heading\|recursive\|slide\|sheet`; `--parallel N`; `--force`; `-y/--yes`; `--json`; `-v` |
| `inspect` | Estimate size/pages/cost — no parsing | ALWAYS first for large/unknown inputs (Vision = 1 call/page) | `--json` |
| `refine` | Markdown → human-readable copy | Only when user wants a readable version; NOT a RAG step | `--skill refine_default\|refine_faithful\|refine_html`; `-o` |
| `doctor` | Check env / deps / API keys | After install or on failure | (none) |

**graph** (`docingest graph <cmd>`, needs `[graph]`, pricier than Vision — only for "X↔Y relationships" / corpus-wide themes / multi-hop; for single facts your Grep on `sources/*.md` is cheaper)

| Command | What it does | Key flags |
|---|---|---|
| `build` | Build knowledge graph on `chunks.jsonl` | `--mode vector_only\|full`; `--force`; `--enrich-chunks` |
| `query` | Ask the graph | `--kb`; `--mode naive\|local\|global\|hybrid\|mix`; `--top-k` |
| `status` | Is a graph built? + counts | `--json` |
| `enrich` | Replay graph entities → `chunks_enriched.jsonl` (no LLM) | `--json` |

**MCP** (`python -m docingest.mcp_server`) — 7 tools, same behaviour:
`inspect` · `run` · `refine` · `build_graph` · `query_graph` · `graph_status` · `enrich_chunks`
(the 4 graph tools need `[graph]`; `graph_status` takes no `config_overrides`).

**Conventions (all commands)**: JSON→stdout, banner/errors→stderr · exit `0` ok / `1` fail / `2` safety abort · `run` is incremental (avoid `--force`) · fine-grained output control: Python API `outputs=[...]`, CLI/MCP use `--no-chunks` or `config_overrides`.

## Quick start

```bash
pip install -e ".[mcp]"     # core + MCP server; add ,nlp,audio,graph as needed
cp .env.example .env        # fill GEMINI_API_KEY / DASHSCOPE_API_KEY
docingest doctor            # checks deps, tools, API keys
docingest run ./docs/ -o ./knowledge/
```

## Workflow

For unknown / large inputs, **inspect for cost first, then run**:

1. `inspect(paths)` → check `est_cost_usd` and `recommendation`
2. `run(paths, output_dir)` → process (incremental; second run hits cache, seconds)
3. Browse outputs: `index.json` (file list) → `sources/*.md` (grep/read) → `chunks.jsonl` (downstream RAG)
4. Optional: `refine(files, skill="refine_faithful")` → human-readable copy

**On safety abort** (`safety.mode=strict` flags an over-budget run): report the
violation summary to the user FIRST — don't blindly retry with `--yes` /
`acknowledge_large=True`.

Full features, config, Python API, MCP client setup: [README.md](README.md).
