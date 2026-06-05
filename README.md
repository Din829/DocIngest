# DocIngest

Universal document preprocessing for RAG and Agentic Search.

Accepts any document (PDF/PPT/Excel/HTML/images/audio/video/ZIP/URLs/...) → parses with Docling + Vision AI + ASR → outputs clean Markdown + chunks + a knowledge map. One preprocessing pipeline, two consumers: **RAG** (vector search on `chunks.jsonl`) and **Agentic Search** (grep/glob on `sources/*.md`).

## Outputs

| File | Purpose |
|---|---|
| `sources/*.md` | Clean Markdown with frontmatter (Agentic Search, grep/glob) |
| `chunks.jsonl` | Chunked text with metadata (RAG vector search) |
| `index.json` | File directory (Agent file discovery) + per-file PDF bounding boxes |
| `knowledge_map.yaml` + `knowledge_search.SKILL.md` | Auto-generated search guide |
| `quality_report.json` | Vision accuracy health check (`[?]` + `[unreadable]` scan) |
| `readable/*.md` | Human-readable version (optional, via `refine`) |
| `graph/` | Knowledge graph artefacts (optional, via `docingest graph build` — see [GraphRAG](#graphrag-optional)) |
| `chunks_enriched.jsonl` | Same chunks as `chunks.jsonl` but with graph entity descriptions injected, for traditional vector RAG (optional, via `docingest graph enrich` or `--enrich-chunks`) |

## Install

Use the helper script — it installs **CPU-only torch first** (docling drags the
~5.6GB CUDA wheel otherwise; DocIngest is CPU inference only), then a
lightweight default extras set (`mcp,audio,nlp,graph` — no GPU libraries):

```bash
git clone https://github.com/Din829/DocIngest.git
cd DocIngest

./scripts/install_python_deps.sh      # Linux / macOS
.\scripts\install_python_deps.ps1     # Windows (PowerShell)

cp .env.example .env                  # Fill in API keys
docingest doctor                      # Check what's missing
python scripts/verify_deps.py         # Build gate — also flags a CUDA torch
```

> **Why not a plain `pip install -e .`?** docling pulls torch transitively, and
> on Linux the default PyPI wheel is the ~5.6GB CUDA build that DocIngest never
> uses. The script forces the CPU wheel up front so the install stays small.
> If you must install by hand, run the CPU-torch line FIRST:
> `pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu`
> then `pip install -e .`. `verify_deps.py` will tell you if a CUDA torch slipped in.

Optional extras — add **one** only when you need that specific feature (there is
no "install everything" path: it would pull GPU torch + backends you don't use):

```bash
pip install -e ".[nlp]"              # Japanese keyword extraction (SudachiPy)
pip install -e ".[mcp]"              # MCP Server (FastMCP)
pip install -e ".[audio]"            # Audio transcription (DashScope Qwen3-ASR)
pip install -e ".[graph]"            # Optional GraphRAG layer (LightRAG)
pip install -e ".[graph-local]"      # Local embedding model — adds ~2GB torch libs
pip install -e ".[graph-gemini]"     # Gemini embeddings for GraphRAG (google-genai SDK)
```

Optional system tools (auto-detected, gracefully skipped if absent):

```bash
# LibreOffice — enables Vision enrichment for Excel/Word/PPT
winget install TheDocumentFoundation.LibreOffice   # Windows
brew install --cask libreoffice                    # macOS
sudo apt install libreoffice                       # Linux

# ffmpeg — enables video audio extraction + long audio segmentation
winget install Gyan.FFmpeg                         # Windows
brew install ffmpeg                                # macOS

# yt-dlp — enables YouTube/Bilibili/video URL processing
pip install yt-dlp

# magika — enables content-based file type detection (optional, ~25MB)
pip install magika
```

Run `docingest doctor` at any time to check your environment.

For automated deployment (CI / Docker / new machine), see [`scripts/`](scripts/):

```bash
./scripts/install_python_deps.sh        # pip extras wrapper
./scripts/install_system_deps.sh        # Linux/macOS — soffice/ffmpeg/poppler/exiftool/node
./scripts/install_system_deps.ps1       # Windows — same via winget + manual poppler
python scripts/verify_deps.py           # true gate (exits non-zero on missing deps —
                                        # docingest doctor never does, so CI can't use it)
```

`Dockerfile.example` at the repo root is a single-stage production template that
wires these scripts together (system bins layer → pip layer → OCR model pre-download
as root → `verify_deps.py` build gate → non-root user).

### Use as a dependency of another project

To embed DocIngest into another Python project (private / in-tree — no public package index needed), drop the source tree into the consumer project and install it in editable mode. The consumer gets a regular `import docingest` with all Python dependencies auto-resolved; edits to the DocIngest source are picked up on next run without reinstalling.

```bash
# From the consumer project root
cp -r /path/to/DocIngest ./vendor/DocIngest
pip install -e "./vendor/DocIngest[mcp,audio,nlp]"   # extras optional
docingest doctor                                     # verify environment
```

System tools (LibreOffice / ffmpeg / yt-dlp) and API keys are **per-machine, not per-project** — each host that runs the consumer needs them installed and configured the same way as a standalone DocIngest install above. API keys can be injected at call-time via Provider classes (see [Python Library](#python-library)) so consumer projects don't need to touch `.env`.

When you update DocIngest, sync the source tree into `vendor/DocIngest/` again (no reinstall needed); only re-run `pip install -e ...` if `pyproject.toml`'s dependency list changed.

## Usage

### Process documents → knowledge base

```bash
# Documents
docingest run ./docs/ -o ./knowledge/
docingest run report.pdf slides.pptx -o ./knowledge/

# Audio / video (local files)
docingest run meeting.mp3 interview.wav -o ./knowledge/
docingest run presentation.mp4 -o ./knowledge/

# Video platform URLs (YouTube, Bilibili, etc.)
docingest run "https://www.bilibili.com/video/BVxxx" -o ./knowledge/
docingest run "https://www.youtube.com/watch?v=xxx" -o ./knowledge/

# Mixed input (files + directories + URLs + ZIPs)
docingest run ./docs/ archive.zip "https://youtube.com/..." -o ./knowledge/
```

Options:
```
-o, --output PATH    Output directory (default: ./knowledge/<input-name>/ for single input, ./knowledge/ for mixed)
-c, --config PATH    Project config YAML
--strategy TEXT      Override chunking strategy: auto | heading | recursive | slide | sheet
                     (auto picks heading/recursive/slide/sheet/timestamp/whole by file format)
--no-chunks          Only output Markdown, skip chunks.jsonl
--parallel INTEGER   Worker count for Vision API calls and ASR segmentation
                     (file-level parallelism is not yet implemented)
--force              Ignore cache, full rebuild
```

**Incremental mode is on by default.** Second run skips unchanged files. All outputs (index.json, chunks.jsonl, knowledge_map, SKILL.md) are fully regenerated each run to include both cached and new files:

```bash
docingest run ./docs/                 # run 1: full pipeline
docingest run ./docs/                 # run 2: 100% cache hit, seconds
docingest run ./docs/ --force         # ignore cache, full rebuild
```

### Inspect documents before processing

Pre-flight check — reports file size, page count, and recommendations without parsing:

```bash
docingest inspect ./docs/             # Rich table output (for humans)
docingest inspect report.pdf --json   # JSON output (for Agents / MCP)
```

### Refine for human readability (optional)

```bash
docingest refine ./knowledge/sources/spec.md                        # Default: readability-first
docingest refine ./knowledge/sources/*.md --skill refine_faithful   # Faithful: word-for-word, only dedup + format
docingest refine ./knowledge/sources/spec.md --skill refine_html    # HTML: fidelity-preserving HTML5 fragment (.html)
```

Available skills: `refine_default` (allows rewriting) | `refine_faithful` (preserves original text exactly) | `refine_html` (same fidelity as faithful, outputs HTML fragment with `.html` extension)

### Visualize parse layout (QA / debugging)

Draw Docling's element bounding boxes onto the rendered page images — a quick way to check parse quality (tables / titles / figures detected correctly?). Reads `index.json` + `assets/`, writes annotated PNGs to a `viz/` subdir. Needs a knowledge base built with `output.include_bounding_boxes` (default on).

```bash
docingest visualize ./knowledge/                        # all pages, every label
docingest visualize ./knowledge/ --pages 1,3 --numbers  # only pages 1 & 3, tag reading order
docingest visualize ./knowledge/ --labels table         # only table boxes
```

### LangChain integration (optional)

Load a knowledge base's chunks **straight into LangChain** as `Document` objects — reusing DocIngest's semantic chunks instead of re-splitting with a naive character splitter. Because LangChain itself integrates dozens of vector stores / retrievers (Azure AI Search, Bedrock Knowledge Bases, Pinecone, ...), this one adapter bridges DocIngest to all of them. The extra pulls only `langchain-core`:

```bash
pip install -e ".[langchain]"
```

```python
from docingest.integrations.langchain import DocIngestLoader

docs = DocIngestLoader("./knowledge/").load()   # -> list[langchain_core.documents.Document]
vectorstore.add_documents(docs)                  # any LangChain backend — you own embeddings / index / config
```

### Python Library

DocIngest exposes a small, stable Python API for use as a dependency of other projects. The public surface is exactly: `ingest`, `inspect`, `refine`, `IngestResult`, `build_config`, and the Provider classes — everything else under `docingest.*` is internal.

```python
import docingest

# Minimal — one line
result = docingest.ingest("./docs/", output="./kb/")
print(result.stats["successful"], "files processed")
for md in result.markdown_files:
    print(md["path"], "→", len(md["content"]), "chars")

# Select only the outputs you need (skips disabled stages entirely)
result = docingest.ingest(
    "./docs/",
    output="./kb/",
    outputs=["markdown", "chunks"],     # no knowledge_map / quality_report / run_log
)
for chunk in result.chunks:
    embed(chunk["text"])                # feed straight into your RAG pipeline

# Inject LLM credentials without touching env vars / .env
result = docingest.ingest(
    "./docs/",
    output="./kb/",
    vision=docingest.GeminiProvider(api_key="..."),
    audio=docingest.DashScopeProvider(api_key="..."),
)

# Cloud LLM providers — same shape, only the Provider class changes.
# (Required fields vary by cloud; unset optional fields fall back to
# ambient credentials — container IAM role / workload identity / etc.)

# Azure OpenAI: deployment-based routing
result = docingest.ingest(
    "./docs/", output="./kb/",
    vision=docingest.AzureOpenAIProvider(
        model="my-gpt4-deployment",                # Azure deployment name, NOT a model id
        api_base="https://my-resource.openai.azure.com/",
        api_version="2024-08-01-preview",
        api_key="...",                              # or set AZURE_API_KEY
    ),
)

# AWS Bedrock: minimal form relies on ambient creds (IAM role on
# EC2/ECS/EKS, or AWS_* env vars set externally)
result = docingest.ingest(
    "./docs/", output="./kb/",
    vision=docingest.BedrockProvider(
        model="anthropic.claude-sonnet-4-20250514-v1:0",
        # aws_access_key_id / aws_secret_access_key / aws_region_name
        # are all optional — pass them only when not using ambient auth.
    ),
)

# Google Vertex AI: project + location mandatory, credentials optional
# (falls back to GOOGLE_APPLICATION_CREDENTIALS env / gcloud ADC /
# GKE workload identity if omitted)
result = docingest.ingest(
    "./docs/", output="./kb/",
    vision=docingest.VertexAIProvider(
        model="gemini-2.5-pro",
        vertex_project="my-gcp-project",
        vertex_location="us-central1",
        # vertex_credentials="/path/to/sa.json"   # optional
    ),
)

# Any config/default.yaml value can be overridden per call (flat dot-path
# form OR nested dict, both work — mix freely)
result = docingest.ingest(
    "./docs/",
    output="./kb/",
    config_overrides={
        "parsing.vision.max_pages": 200,
        "chunking.max_tokens": 1024,
    },
)
```

**Progress callback (optional)** — pass `on_progress=...` to receive one event per file completion (cached / added / updated / failed / skipped). Useful when piping progress to a UI or SSE stream. The callback runs synchronously on the pipeline thread; exceptions are swallowed (logged at warning level) so a buggy callback can't break the run:

```python
def on_event(e):
    print(f"[{e['current']}/{e['total']}] {e['file']} — {e['status']}")

docingest.ingest("./docs/", output="./kb/", on_progress=on_event)
```

**Signal handling** — by default DocIngest does NOT install a SIGINT handler when used as a library, so embedding it in a long-running host (web server, daemon) leaves your own Ctrl+C handling intact. The CLI opts in to graceful Ctrl+C (`install_signal_handler=True`); library callers can do the same explicitly when running stand-alone.

**Return value** — `IngestResult` carries the produced artefacts so callers don't have to re-read the output directory:

| Field | Populated when | Shape |
|---|---|---|
| `markdown_files` | `"markdown"` in outputs | `[{"path", "content", "metadata"}, ...]` |
| `chunks` | `"chunks"` in outputs | `[{"id", "text", "metadata"}, ...]` |
| `index` | `"index"` in outputs | content of `index.json` |
| `knowledge_map` | `"knowledge_map"` in outputs | parsed `knowledge_map.yaml` |
| `quality_report` | `"quality_report"` in outputs | parsed `quality_report.json` |
| `stats` | always | `total_files`, `successful`, `failed`, `token_usage`, `errors`, `warnings`, `quality`, `safety`, `interrupted` |
| `output_dir` | always | absolute path the run wrote to (handy for later CLI ops) |

(`run_log` is a valid `outputs=` value — it toggles writing `log.md` on disk — but it is not a field on `IngestResult`; the run history lives in the file, not the return object.)

**API stability** — only names re-exported from `docingest/__init__.py` are public. Internal modules (`docingest.pipeline`, `docingest.parsers`, `docingest.chunkers`, hooks, output writers) may change between minor versions.

**Advanced — lower-level API** (use only when you need direct control over parser/chunker instantiation; the facade above covers 95% of use cases):

```python
from pathlib import Path
from docingest.config import load_config
from docingest.parsers import create_parser
from docingest.chunkers import create_chunker
from docingest.pipeline import run_pipeline

config = load_config(cli_overrides={"output": {"dir": "./knowledge"}})
parser = create_parser(config)
chunker = create_chunker(config)
result = run_pipeline([Path("./docs")], config, parser, chunker)
print(f"{result.successful}/{result.total_files} files, {result.total_chunks} chunks")
```

### How agents discover commands

One command catalog (CLI / graph / MCP, three sections) is the single source of
truth, surfaced on every channel an agent enters through — so it sees the full
command set no matter how it calls DocIngest:

- **Agent Skill** `.claude/skills/docingest/SKILL.md` — its frontmatter
  `description` lists the whole command set and is auto-loaded into context
  (~100 tokens, zero action), so the agent knows the commands exist before
  reading anything; the body holds the full table. Agent Skills are an
  [open standard](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
  (Claude.ai / Claude Code / API), so this isn't Claude-Code-specific.
- **AGENTS.md** — same table at the top, for agents reading the repo directly
  or environments without skill support.
- **MCP `instructions`** — the at-a-glance tool list, for MCP callers.
- **`docingest --help`** — the command set at the top, for shell/agent callers.

Disclosure is progressive: the skill `description` (≈100 tokens, always) → the
full table in `SKILL.md` / AGENTS.md (on demand) → `docingest <cmd> --help` for
per-flag detail. The catalog is hand-written but pinned to the code by
`tests/unit/test_command_catalog.py`, which asserts the documented command set,
`--strategy` values, and `refine` default match the source — change a command
without updating the table and the test goes red.

> Not to be confused with `skills/refine_*.SKILL.md` (LLM prompts for the
> `refine` command) or the generated `<output>/knowledge_search.SKILL.md`
> (a per-knowledge-base search guide for downstream agents) — both reuse the
> `SKILL.md` filename but are unrelated to the Agent Skill above.

### MCP Server (for AI Agents)

Thin MCP wrapper — exposes DocIngest as tools for AI Agents (Claude / GPT / etc.).
`mcp_server.py` is a transport layer only; every tool is a ~10-line wrapper around the corresponding DocIngest Python API.

**Install:**
```bash
pip install -e ".[mcp]"
```

**Run:**
```bash
python -m docingest.mcp_server                    # stdio (Claude Desktop, Claude Code, VS Code Copilot)
python -m docingest.mcp_server --transport http   # Streamable HTTP (web clients — recommended)
python -m docingest.mcp_server --transport sse    # SSE (legacy in fastmcp v3; kept for back-compat)
```

**Available tools** (every tool accepts optional `config_overrides` for dynamic behavior). Tools that process documents route through the public Python facade so MCP and library callers always share the same behaviour:

| Tool | Purpose | Backed by |
|---|---|---|
| `inspect` | Pre-flight check (size, pages, cost estimate) | `docingest.inspect()` |
| `run` | Process documents → knowledge base | `docingest.ingest()` |
| `refine` | AI-powered Markdown cleanup | `docingest.refine()` |
| `build_graph` | Build / extend knowledge graph (opt-in, requires `[graph]`) | `docingest.graph.build()` |
| `query_graph` | Query the graph (local / global / hybrid / mix / naive) | `docingest.graph.query()` |
| `graph_status` | Inspect graph build state + entity / relation / community counts | `docingest.graph.status()` |
| `enrich_chunks` | Replay graph entities into `chunks_enriched.jsonl` so traditional vector RAG also benefits | `docingest.graph.enrich_chunks()` |

Browsing / searching / reading the produced knowledge base is **NOT exposed as MCP tools by design** — DocIngest is a preprocessing engine, not a retrieval engine. Use your agent's native Grep / Read / Glob on the on-disk artefacts (`sources/*.md`, `index.json`, `chunks.jsonl`). Each knowledge base ships an auto-generated `knowledge_search.SKILL.md` — Read it once at the start of a session and you have the corpus's summary, file index, keyword index, and a language-routed search protocol.

The four graph tools are registered **only when `lightrag-hku` is installed** (`pip install -e ".[graph]"`); without the extras they don't appear in the tool listing and the rest of the server is unaffected.

**Client configuration:**

*Claude Desktop* — edit `claude_desktop_config.json` (Settings > Developer > Edit Config), restart Claude fully (quit, not just close window):
```json
{
  "mcpServers": {
    "docingest": {
      "command": "python",
      "args": ["-m", "docingest.mcp_server"],
      "cwd": "/path/to/DocIngest"
    }
  }
}
```

*Claude Code* — add to `.mcp.json` at project root (shared via git) or `~/.claude.json` (personal, all projects):
```json
{
  "mcpServers": {
    "docingest": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "docingest.mcp_server"]
    }
  }
}
```

*VS Code (Copilot)* — add to `.vscode/mcp.json` (key is `servers`, **not** `mcpServers`):
```json
{
  "servers": {
    "docingest": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "docingest.mcp_server"]
    }
  }
}
```
MCP tools appear in Copilot Agent mode (Ctrl+Alt+I).

**Typical Agent workflow:**
```
1. inspect(["./new_docs/"])                      → size / pages / cost estimate (MCP tool)
2. run(["./new_docs/"])                          → incremental processing      (MCP tool)
3. Read  ./knowledge/knowledge_search.SKILL.md   → summary + search protocol   (native Read)
4. Read  ./knowledge/index.json                  → file inventory              (native Read)
5. Grep  "契約" ./knowledge/sources/             → find by keyword             (native Grep)
6. Read  ./knowledge/sources/contract.md         → read full file              (native Read)
7. refine(["./knowledge/sources/contract.md"])   → optional AI cleanup         (MCP tool)
```

Steps 3-6 use the agent's own Grep / Read tools — DocIngest deliberately does not wrap them, so each agent (Claude Code / Cursor / Codex / Copilot / ...) gets to use the file-exploration tooling it already has, with whatever flags / context / output formatting it does best.

**Config overrides from Agent** — override any config path per call without touching files. Two equivalent forms, mix freely:

```python
# Nested dict form (verbose, groups well when you override several keys under one section)
run(["docs/"], config_overrides={
    "parsing": {"vision": {"max_pages": 200, "triage": {"enabled": True}}},
    "chunking": {"strategy": "heading", "max_tokens": 1024},
    "sanitize": {"enabled": True},
})

# Flat dot-path form (compact, one line per override)
run(["docs/"], config_overrides={
    "parsing.vision.max_pages": 200,
    "parsing.vision.triage.enabled": True,
    "chunking.strategy": "heading",
    "chunking.max_tokens": 1024,
    "sanitize.enabled": True,
})
```

**Adding a new tool** — open `src/docingest/mcp_server.py`, add a `@mcp.tool` function that delegates to a function in `docingest.api` (keep MCP a thin transport layer — the facade is the single source of truth for processing logic). FastMCP auto-generates the schema from type hints + docstring. See ARCHITECTURE.md §3.3 for the public-API contract and §3.2 for the wider code map.

**Troubleshooting:**
- "Module not found" → `pip install -e ".[mcp]"`
- API key errors → set `GEMINI_API_KEY` / `DASHSCOPE_API_KEY` in `.env` or environment. As an alternative, library callers can inject keys directly via Provider classes (`docingest.GeminiProvider(api_key="...")`) without touching env vars — see the Python Library section above.
- Large files hang → run `inspect` first and use `config_overrides` to raise `max_pages`

## GraphRAG (optional)

Build an entity / relation knowledge graph on top of an existing knowledge base, then run global / local / hybrid queries via [LightRAG](https://github.com/HKUDS/LightRAG). **Strictly opt-in** — `docingest run` never touches it, and the import path `docingest.graph` only loads when explicitly imported.

> **Note on communities** — LightRAG ≥ 1.4 no longer generates community reports automatically during `ainsert`; the build step produces entities + relations + per-entity / per-relation embeddings. The `global` query mode still works (it falls back to relation-level retrieval), but you won't see community summaries in `status` output — `Communities = 0` is expected, not a bug.

```bash
# 1. Install the optional extras (LightRAG + OpenAI embedding client)
pip install -e ".[graph]"

# 2. Process documents as usual
docingest run ./docs/ -o ./kb/

# 3. Build the graph on top of the knowledge base
docingest graph build ./kb/                          # full mode (default)
docingest graph build ./kb/ --mode vector_only       # cheap: skip community detection

# 4. Query
docingest graph query "整个语料的主要主题是什么？" --kb ./kb/ --mode global
docingest graph query "X 和 Y 什么关系？"            --kb ./kb/ --mode local
docingest graph query "comprehensive answer please" --kb ./kb/ --mode hybrid
docingest graph status ./kb/                         # entity / relation / community counts
```

Two retrieval modes:

| Build mode | Cost | Query modes available | Best for |
|---|---|---|---|
| `vector_only` | Cheap (skips community summary LLM calls) | `naive`, `local` | Single-fact / two-hop questions |
| `full` (default) | Higher (per-community LLM summary) | `naive`, `local`, `global`, `hybrid`, `mix` | Multi-hop reasoning, "main themes / trends" |

Python library:

```python
import docingest
import docingest.graph                 # explicit import — never triggered by `import docingest`

# Build (incremental — second run reuses cache for unchanged chunks)
result = docingest.graph.build(
    "./kb/",
    mode="full",
    llm=docingest.OpenAIProvider(api_key="...", model="gpt-5.4-mini"),
    embedding=docingest.graph.OpenAIEmbedding(
        api_key="...",
        model="text-embedding-3-small",
        dimension=1536,
    ),
    config_overrides={"graph.lightrag.entity_extract_max_gleaning": 2},
)
print(result.entities_count, "entities,", result.relations_count, "relations")

# Query
answer = docingest.graph.query(
    "What are the main themes across the corpus?",
    knowledge_dir="./kb/",
    mode="hybrid",
)
print(answer.answer)
```

Outputs land under `./kb/graph/` (LightRAG's working_dir layout); deleting the folder leaves the rest of the knowledge base intact. Extraction is incremental — chunks whose content + LLM config hash hasn't changed since last build are skipped.

Embedding providers (`docingest.graph.OpenAIEmbedding` / `GeminiEmbedding` / `SentenceTransformerEmbedding`) are independent from the main pipeline's Vision / ASR providers — pick a small / cheap model just for graph extraction without affecting `docingest run`. Sentence-transformers (`pip install -e ".[graph-local]"`) gives zero-API-cost local embeddings.

**Credential resolution order** (highest wins):

1. Explicit `Provider(api_key="...")` argument
2. Environment variable (e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`)
3. `.env` file in the working directory (auto-loaded **on the CLI path only**)
4. YAML config

**Library callers** (`docingest.graph.build(...)`) do NOT auto-load `.env` — by design, so embedding DocIngest into a long-running host doesn't pollute the process environment. If you want `.env` behaviour from the library, call `load_dotenv()` yourself before invoking the facade, or pass keys via Provider objects.

**Known issue — repeated `query()` in the same Python process.** LightRAG 1.4's internal `asyncio.Lock` is bound to the first event loop, so a fresh `asyncio.run()` per call (which is what `docingest.graph.query()` does) fails on the 2nd+ invocation with "Lock bound to a different event loop". We handle this differently per entry point:

| Entry point | Multiple calls per process? | Status |
|---|---|---|
| **CLI** (`docingest graph query`) | No — each invocation is a fresh subprocess | ✅ Just works, nothing to do |
| **MCP server** (`build_graph` / `query_graph` tools) | Yes — long-running server, agents call repeatedly | ✅ Auto-fixed: the MCP entry point applies `nest_asyncio` so subsequent `asyncio.run()` calls reuse the existing loop. Agents see no difference |
| **Python library** (`docingest.graph.query(...)`) | Depends on caller | 🟡 Not auto-fixed (we don't want to monkey-patch host's asyncio). Three workarounds: (1) call `nest_asyncio.apply()` yourself before looping over queries; (2) spawn one subprocess per query; (3) issue a single call per process |

When the bug DOES bite (library path without workaround), the empty answer is surfaced as `result.stats["error"]` with a descriptive message — you can detect failure programmatically, you just can't recover from it without the workaround.

### Boost traditional RAG with graph entities (`chunks_enriched.jsonl`)

GraphRAG queries are great, but most teams already have a vector RAG pipeline they want to keep. The optional **chunk enrichment** stage feeds the graph's extracted entity names + descriptions back into a NEW jsonl file — giving your existing vector RAG a precision boost without rewriting it.

```bash
# Either as a follow-up of build:
docingest graph build ./kb/ --enrich-chunks

# Or standalone (graph already built earlier):
docingest graph enrich ./kb/
```

What it does:
- Reads `graph/vdb_entities.json` and the chunk-id map (no LLM calls).
- For each chunk, picks the top-N most relevant entities (entities that occur **only** in this chunk are prioritised, then shorter names — longer names tend to be doc-level boilerplate).
- Writes a sibling `chunks_enriched.jsonl` with two channels of enrichment:
  - **Text channel** — injects `[关键实体: 敷金 — 預かり金として扱われる費用; ...]` right after the `[来源: ...]` header. Pure vector RAG benefits because the embedding model now sees explicit anchors for the entities, easing synonym recall ("修繕費" finds the chunk where the entity description mentions "修繕" even though the surface form differs).
  - **Metadata channel** — adds `metadata.entities = [{"name", "description", "exclusive"}, ...]`. Hybrid / metadata-filtered RAG (Qdrant `WHERE entities CONTAINS '敷金'`, BM25+vector) can use this directly.

Invariants:
- **The original `chunks.jsonl` is NEVER modified.** Hash-checked by tests.
- **No LLM / embedding calls** during enrichment — pure replay over on-disk graph data. 44 chunks ≈ 35 ms.
- Re-running replaces (not stacks) the previous injection. Deterministic output for the same input.

Tune via config:
```yaml
graph:
  enrich_chunks:
    enabled: false                # default OFF
    max_entities_per_chunk: 5
    max_description_length: 100
    inject_into_text: true
    inject_into_metadata: true
```

### Speed vs. precision — two knobs that matter

Graph build is dominated by LLM calls during entity extraction — each chunk gets sent to the LLM at least once with a ~16K-character prompt (LightRAG's own template + few-shot examples + your chunk content, ~88% of which is the fixed template overhead). Two YAML knobs let you trade speed against extraction recall:

```yaml
graph:
  lightrag:
    entity_extract_max_gleaning: 0  # DocIngest default: skip the 2nd "did you miss anything?" pass
    max_parallel_insert: 4          # chunks processed in parallel; LightRAG's own default is 2
```

| Knob | Default | When to raise | When to lower |
|---|---|---|---|
| `entity_extract_max_gleaning` | **0** | Set to `1` (or `2`) for **prose / academic / legal** corpora where the first extraction misses ~10-15% of edge entities. Doubles LLM cost per chunk. | Already at floor. |
| `max_parallel_insert` | **4** | `6-8` if your LLM tier supports high RPM (helps wall-clock proportionally). Zero effect on precision — pure throughput knob. | `1-2` if you share an OpenAI tier-1 key with other workloads (avoids rate-limit retries). |

Why the DocIngest default for gleaning is **0** (different from LightRAG's `1`): the typical input here is a **structured document** — DB design sheets, API specs, Q&A tables, contracts — where the first extraction already captures 95%+ of meaningful entities. Gleaning doubles LLM calls for a marginal recall bump that's mostly irrelevant on this input shape. If you're feeding free-flowing prose, raise it back to `1`.

A small (~50 chunk) corpus typically rebuilds in **2-3 minutes** with these defaults vs. **6-8 minutes** at LightRAG's defaults — same precision on structured input, half the LLM bill.

## Configuration

Four layers, highest wins:

```
CLI args  >  environment variables  >  project docingest.yaml  >  config/default.yaml
```

### YAML

```yaml
chunking:
  strategy: "heading"
  max_tokens: 1024

  # Heading merge behaviour — all three policies are independently tunable
  # so different document types (legal contracts, tech reports, tutorials)
  # can use different trade-offs without code changes.
  heading:
    prelude_policy: "attach_to_first"     # attach_to_first | standalone | drop
    orphan_heading_policy: "merge_forward" # merge_forward | keep
    title_path_strategy: "deepest"         # deepest | first | join_all

  protection:
    # What to do when a protected block (table/code/list/quote) exceeds
    # its allowed_overflow × max_tokens budget. Per-block-type control.
    on_overflow:
      table: "row_split"      # split giant tables at data-row boundaries,
                              # repeat header in every sub-chunk
      code_block: "bypass"    # don't split code — breaks syntax
      list: "bypass"
      default: "bypass"
    table_split:
      keep_header_in_every_chunk: true
      max_rows_per_chunk: null  # null = only token budget applies

models:
  defaults:
    max_response_tokens: 32768  # Global fallback — every LLM task inherits this
                                # (vision / chunking_assist / contextual_summary / ...)
                                # unless it sets its own max_response_tokens.
    retry_on_truncation: true   # Retry once when finish_reason=="length"
    retry_max_tokens: 65536
  vision:
    max_response_tokens: 32768  # Per-task override (optional — inherits defaults if unset)
    primary: { provider: "google", model: "gemini-3-flash-preview" }
  audio_transcription:
    primary: { provider: "dashscope", model: "qwen3-asr-flash" }
    fallback: { provider: "openai", model: "whisper-1" }

parsing:
  vision:
    image_dpi: 180
    supplement_only: false      # Global default: Vision transcribes the whole page.
                                # xlsx flips to true below (supplement only —
                                # openpyxl already has the table, Vision just adds
                                # visuals → no Docling↔Vision dup). PDF/PPT stay
                                # false: Docling fragments 方眼紙 tables, so they
                                # need full transcription to recover the body.
    triage:
      enabled: true             # Skip pure-text pages (saves Vision API cost)
  pdf:
    hidden_text_detection:
      enabled: true             # Detect invisible/background content
    vision:                     # Optional per-format Vision override (model / max_response_tokens / image_dpi).
      image_dpi: 220            # Unset fields fall through to global models.vision.* / parsing.vision.*.
  xlsx:
    vision:
      supplement_only: true     # Vision adds visuals only, never re-transcribes
                                # the openpyxl-rendered table (removes xlsx dup).
  pptx:
    vision:
      max_response_tokens: 8192 # PPT pages are content-light — cap output to save cost.
  audio:
    prefer_subtitles: true
    language: "auto"
    max_segment_seconds: 150
  url:
    enabled: true
  zip:
    enabled: true
  magika:
    enabled: true

output:
  include_bounding_boxes: true  # Per-element coordinates for RAG citation

sanitize:
  enabled: true                 # PII masking (email, URL, credit card, IP, phone)

incremental:
  enabled: true
```

### Environment variables

Any config value can be overridden by `DOCINGEST__<path>` env variables:

```bash
export DOCINGEST__chunking__max_tokens=1024
export DOCINGEST__models__vision__primary__model=gemini-3-pro-preview
export DOCINGEST__parsing__audio__language=ja
```

## Pipeline at a Glance

```
input files / dirs / URLs / ZIPs
      │
      ▼
discover_files  (ZIP expansion, yt-dlp for URLs)
      │
      ▼
Phase 0.5: legacy .xls/.doc/.ppt → .xlsx/.docx/.pptx via LibreOffice
            (opt-out: parsing.<xls|doc|ppt>.auto_convert_to_*)
      │
      ▼
partition by incremental cache  (skip unchanged files)
      │
      ▼
for each new file:
      ├─ pre_parse hook        DOCX OMML → LaTeX
      ├─ Docling / Media / Text
      │     ↳ xlsx pre-route: openpyxl renderer (default ON) —
      │       per-sheet headings, merged-cell anchor-only, empty
      │       columns pruned. Bypasses Docling's Excel backend to
      │       guarantee correct title_path on chunks.
      ├─ garbled fallback      glyph< → pymupdf
      ├─ Excel denoise         merged cells, sparse rows
      ├─ LibreOffice pages     xlsx/docx/pptx → PDF → screenshots
      ├─ post_parse hook       PPTX chart direct-read
      ├─ Vision enrichment     per-page, 8-layer triage, parallel; format-split
                                supplement/full — xlsx supplements visuals only
                                (no table re-transcribe), PDF/PPT full whole-page
      ├─ pre_write hook        exiftool / sanitize
      ├─ Vision dedup          OFF by default — duplication is already removed at
                                the source by the format-split supplement above;
                                this old length-ratio post-pass stays opt-in
                                (output.dedup.enabled)
      ├─ write sources/*.md    + assets/
      └─ chunk + path-inject   auto / heading / slide / sheet / timestamp
      │
      ▼
index.json + chunks.jsonl + knowledge_map.yaml + quality_report.json
      │
      ▼  (OPTIONAL, opt-in via `docingest graph build` — see GraphRAG section)
LightRAG entity / relation extraction → graph/  (graphml + entity vdb + chunk vdb)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full Phase breakdown, design rationale, and how to add new formats / hooks / chunkers. The optional graph layer is documented in [ARCHITECTURE.md §10](ARCHITECTURE.md#10-graphrag-子模块docingestgraph可选).

## Key Features

- **20+ formats** via Docling (PDF, DOCX, PPTX, XLSX, HTML, images, Markdown, ...)
- **Audio/video transcription** — subtitle-first (SRT/VTT, zero API cost), Qwen3-ASR-Flash default, OpenAI Whisper fallback. Long audio auto-segmented.
- **Video URL support** — YouTube, Bilibili, and 1000+ platforms via yt-dlp. Auto subtitle + metadata extraction.
- **ZIP archive expansion** — recursive unpacking with Japanese filename recovery, bomb protection.
- **Per-page Vision AI** — AI decides per page: clean up text / describe charts / OCR scan. Parallel execution, cached by content hash.
- **Format-aware Vision supplement** — xlsx (rendered cleanly by openpyxl) lets Vision SUPPLEMENT only the visual content (charts / pictures / stamps) and never re-transcribe the table, removing the Docling↔Vision duplication **at the source** (measured: per-section re-transcription 65–92% → 0–5%, and body text is never dropped because it lives in the openpyxl render the supplement can't touch). PDF/PPT stay on full whole-page transcription — Docling fragments their 方眼紙 tables, so they need it to recover the body. Per-format via `parsing.<format>.vision.supplement_only` (global default off, xlsx on).
- **PPTX chart direct-read** — python-pptx extracts chart data (categories, series, values) as 100% accurate Markdown tables. Vision supplements with visual context.
- **DOCX math equations** — OMML → LaTeX preprocessing before Docling parses. `$E=mc^{2}$` instead of garbled text.
- **Smart chunking** — auto strategy by format (heading/recursive/slide/sheet/timestamp). CJK-aware token estimation. Protected blocks with per-type overflow control (tables, code, lists) and per-type `on_overflow` strategy — oversized Markdown tables are split at data-row boundaries with the header repeated in every sub-chunk (2026 industry standard, handles Docling's merged-cell expansion). Single-pass heading merge (prelude + orphan-heading + small-section policies) produces zero-fragment chunks with the deepest-available title_path. Adjacent byte-identical chunks auto-deduplicated. All behaviour is config-driven — every knob in `chunking.heading.*` and `chunking.protection.*`.
- **Excel via openpyxl (default)** — xlsx is rendered by `openpyxl` instead of Docling: every sheet's body lives under its own `## SheetName` heading (so chunk `title_path` always points at the right sheet), merged cells stay anchor-only (no N×N value duplication), entirely-empty columns are pruned out of wide layouts. Embedded pictures pasted into cells (PNG/JPEG/GIF/BMP/TIFF/WebP **and** EMF/WMF — read directly from the xlsx OOXML structure, bypassing openpyxl's silent EMF drop) are anchored to their actual row with a `<!-- image: <filename> -->` marker so Vision triage can pick them up and downstream RAG / Agentic Search can locate them. Falls back to Docling automatically if openpyxl is unavailable or the workbook can't open. Disable via `parsing.xlsx.use_openpyxl_renderer: false`.
- **Excel denoising** — merged-cell dedup, sparse row cleanup, embedded image extraction.
- **Legacy Office support (`.xls` / `.doc` / `.ppt`)** — pre-2007 binary Office files are auto-converted to their modern OOXML form (`.xlsx` / `.docx` / `.pptx`) via LibreOffice as Phase 0.5, then routed through the full modern-format path (Docling / openpyxl renderer + Vision + chunking). Docling rejects the binary forms outright, so this conversion is what makes them work at all. The original filename / mimetype / mtime are preserved in `metadata.lineage.original_input`, and a `format_convert` entry is recorded in `metadata.lineage.transformations`. Conversion result is cached at `.cache/_legacy_convert/<sha256>.<ext>` so the same file converts exactly once per output dir. LibreOffice missing → warning + degrades to TextParser fallback (the pipeline never raises). Disable per format via `parsing.<xls|doc|ppt>.auto_convert_to_*: false`.
- **Content-based format detection** — magika ML model identifies files with weak/missing extensions.
- **Anti-hallucination Vision** — `[?]` for partial reads, `[unreadable]` for gaps. Post-run quality report.
- **Vision triage** — per-page analysis skips pure-text pages, saving 30-60% Vision API cost with zero info loss. Eight-layer defence for damaged pages: `glyph<` / `&lt;` CID markers, U+FFFD ratio, complex-table density, CJK mixed-script anomaly, **language-script consistency** (new) — catches CMap failures that produce CLEAN but WRONG Unicode (e.g. Bengali/Thai/Tibetan chars on a Japanese-declared document; the other checks miss this because the output is legal Unicode). Whitelist per language (ja/zh/en/ko by default), add a language = edit `parsing.vision.triage.language_script_check.expected_scripts` — no code change. Default ON (`parsing.vision.triage.enabled`).
- **Bounding boxes** — per-element PDF coordinates extracted from Docling for RAG source citation and highlighting. Exposed per file in `index.json` (`files[].element_boxes[<page_no>] = [{label, bbox, text_preview}, ...]`); RAG apps look them up by matching `chunk.metadata.source` → index entry. Toggle via `output.include_bounding_boxes`.
- **Parse visualization** — `docingest visualize <kb>` draws those element boxes onto the rendered page images (colored by label, optional reading-order numbers) for QA / debugging. PIL on PNG; scales bboxes via the per-page `page_sizes` now stored in `index.json` (falls back to render-DPI for KBs built earlier).
- **Repeating-furniture dedup** — opt-in `pre_write` hook collapses per-page furniture Vision transcribed (e.g. a `DocuSign Envelope ID` repeated on every page) down to its first copy — never deletes every copy, so no unique content is lost. Default OFF (`hooks.strip_repeating.enabled`).
- **LangChain integration** — `DocIngestLoader` (opt-in `[langchain]` extra) maps `chunks.jsonl` → LangChain `Document`, bridging DocIngest to any LangChain vector store / retriever (Azure AI Search, Bedrock, Pinecone, ...) while reusing its semantic chunks. Pulls only `langchain-core`.
- **Chunk lineage** — every chunk in `chunks.jsonl` carries a `metadata.lineage` sub-dict recording `source_markdown`, `original_input` (filename / mimetype / binary_hash / last_modified), and an ordered `transformations` array of what actually shaped it (parser → hooks → vision → chunker). Disabled features (e.g. sanitize.enabled=false) and triaged-out Vision pages are NOT recorded — lineage is a positive provenance trail for RAG citation / quality attribution / reproducibility, not a debug log. Existing flat metadata fields (`source`, `original_file`, `format`, `language`, `title_path`, …) are preserved unchanged for backwards compatibility.
- **Hidden text detection** — flags invisible/background content via Docling ContentLayer analysis.
- **Sensitive data sanitization** — opt-in PII masking (email, URL, credit card with Luhn validation, IPv4, JP phone). High-precision rules only, no name detection. Default OFF (`sanitize.enabled`).
- **Incremental cache** — content-addressed, per-file, crash-safe. 100 docs + 1 new → only 1 re-runs.
- **AI Refine** — standalone `refine` command for human-readable output with Mermaid flowcharts.
- **Knowledge Map** — auto-generated search guide + keyword reverse index. Optional SudachiPy integration for high-precision Japanese keyword extraction (language-routed: Japanese → SudachiPy, Chinese/Korean/English → regex).
- **Multi-provider** — Gemini / OpenAI / Anthropic / DashScope with automatic fallback.
- **Network-level retry** — every LLM call (Vision / text completion / ASR) passes `num_retries` to litellm, which applies exponential backoff on transient errors (rate limits, 5xx, TCP resets). Default 2 retries via `models.defaults.max_retries`; per-task override (e.g. `models.vision.max_retries: 5` for a flaky endpoint). Orthogonal to truncation retry (`retry_on_truncation`) which sits at the application layer.
- **Wall-clock timeouts** — bounded parse and Vision calls so a single hung file can't stall the run. `parsing.timeout_sec` (default 300s) caps Docling per file; `models.vision.timeout_sec` (default 180s) caps each Vision page call. On timeout the file is recorded as failed with `error_type: "timeout"` in `errors.json` and the pipeline continues. Set either to `null` to disable.
- **Graceful interrupt** — Ctrl+C between files lets the current file finish, then writes `chunks.jsonl` / `index.json` / `knowledge_map.yaml` for everything completed so far and exits with code 130. Rerun resumes from the incremental cache. Press Ctrl+C twice for a hard exit.
- **Classified errors** — `errors.json` entries carry an `error_type` field (`timeout` / `parse_error` / `chunk_error` / `io_error` / `interrupted` / `unknown`) so downstream consumers can branch without grepping the message.
- **Per-format Vision overrides** — `parsing.<pdf|pptx|docx|xlsx>.vision` shallow-merges over the global Vision config to tune `model` / `max_response_tokens` / `image_dpi` per format. Raise DPI for dense PDFs, cap output for content-light PPTs, swap models for scans — without affecting other formats. Unset fields fall through to global.
- **Cross-platform binary finder** — auto-discovers LibreOffice, ffmpeg, yt-dlp, exiftool on Windows/macOS/Linux standard paths.
- **Config-driven** — all thresholds, strategies, models in YAML. No hardcoding.

## Project Layout

See [ARCHITECTURE.md §3.1](ARCHITECTURE.md) for the full annotated directory tree and §3.2 for a "I want to look at X → find it at Y" quick-navigation table.

## Testing

All suites below pass cleanly on every supported platform. Run them after any
change to verify no regression.

```bash
# Public Python API (facade + outputs whitelist + Provider injection)
python tests/unit/test_api.py

# Chunk lineage (metadata.lineage + transformations trail)
python tests/unit/test_lineage.py

# Network-level retry plumbing (num_retries → litellm)
python tests/unit/test_retry.py

# Config-overrides layering
python tests/unit/test_config_override.py

# Incremental cache behaviour (modify / delete / config-change scenarios)
python tests/incremental/run_tests.py

# GraphRAG layer — optionality regression (passes with or without [graph] extras)
python tests/unit/test_graph_optional.py

# GraphRAG layer — internals (chunks_loader filters, cache hashing, mode validation)
python tests/unit/test_graph_internals.py

# GraphRAG chunk enrichment (chunks.jsonl preservation, top-N selection, idempotency)
python tests/unit/test_graph_enrich.py
```

`tests/unit/test_mixed.py` exists but currently has a known failure in its
`test_mixed_content` assertion on `title_path`. The failure predates the
current codebase and is tracked separately — skip this suite when verifying
your own changes.

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — Architecture, Phase breakdown, design rationale, extension guide (hooks / parsers / chunkers), known technical debt. **§10** covers the optional `docingest.graph` layer (boundaries, module layout, three-tier caching, swapping backends).
- **[INTEGRATION.md](INTEGRATION.md)** — How to integrate DocIngest into your own system (CLI subprocess / Python library / MCP), per-scenario recipes, cross-cutting concerns
