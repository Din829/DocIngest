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

```bash
git clone https://github.com/Din829/DocIngest.git
cd DocIngest
pip install -e .                     # Core dependencies
cp .env.example .env                 # Fill in API keys
docingest doctor                     # Check what's missing
```

Optional extras (install as needed):

```bash
pip install -e ".[nlp]"              # Japanese keyword extraction (SudachiPy)
pip install -e ".[mcp]"              # MCP Server (FastMCP)
pip install -e ".[audio]"            # Audio transcription (DashScope Qwen3-ASR)
pip install -e ".[graph]"            # Optional GraphRAG layer (LightRAG)
pip install -e ".[graph-local]"      # Add local embedding model (zero API cost)
pip install -e ".[nlp,mcp,audio]"    # All optional Python packages
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
--strategy TEXT      Override chunking strategy: auto | heading | recursive
                     (auto picks slide/sheet/timestamp/whole by file format)
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
```

Available skills: `refine_default` (allows rewriting) | `refine_faithful` (preserves original text exactly)

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
| `stats` | always | `total_files`, `successful`, `failed`, `token_usage`, `errors`, `safety`, ... |

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
python -m docingest.mcp_server --transport sse    # SSE (web clients)
```

**Available tools** (every tool accepts optional `config_overrides` for dynamic behavior). Tools that process documents route through the public Python facade so MCP and library callers always share the same behaviour:

| Tool | Purpose | Backed by |
|---|---|---|
| `inspect` | Pre-flight check (size, pages, cost estimate) | `docingest.inspect()` |
| `run` | Process documents → knowledge base | `docingest.ingest()` |
| `refine` | AI-powered Markdown cleanup | `docingest.refine()` |
| `search_knowledge` | Keyword search on processed knowledge base | grep on `sources/*.md` |
| `list_knowledge` | List knowledge base contents (files, stats) | reads `index.json` |
| `read_source` | Read full content of a source Markdown file | reads `sources/*.md` |
| `build_graph` | Build / extend knowledge graph (opt-in, requires `[graph]`) | `docingest.graph.build()` |
| `query_graph` | Query the graph (local / global / hybrid / mix / naive) | `docingest.graph.query()` |
| `graph_status` | Inspect graph build state + entity / relation / community counts | `docingest.graph.status()` |
| `enrich_chunks` | Replay graph entities into `chunks_enriched.jsonl` so traditional vector RAG also benefits | `docingest.graph.enrich_chunks()` |

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
1. inspect(["./new_docs/"])                      → size / pages / cost estimate
2. run(["./new_docs/"])                          → incremental processing
3. list_knowledge("./knowledge")                 → browse contents
4. search_knowledge("契約", "./knowledge")        → find by keyword
5. read_source("contract.md")                    → read full file
6. refine(["./knowledge/sources/contract.md"])   → optional AI cleanup
```

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

**Known issue — `query()` in the same Python process can silently fail on the 2nd+ call.** LightRAG 1.4's internal `asyncio.Lock` is bound to the first event loop, but `docingest.graph.query()` uses a fresh `asyncio.run()` per call; subsequent invocations hit a "Lock bound to a different event loop" error inside LightRAG. We now detect this — `result.stats["error"]` is populated and the answer is empty — but the underlying bug is upstream. **Workaround**: call from the CLI (`docingest graph query`, one subprocess per call), or spawn one subprocess per query from your Python code. Reusing the same process across many queries will not work until LightRAG fixes the lock binding.

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
    triage:
      enabled: true             # Skip pure-text pages (saves Vision API cost)
  pdf:
    hidden_text_detection:
      enabled: true             # Detect invisible/background content
    vision:                     # Optional per-format Vision override (model / max_response_tokens / image_dpi).
      image_dpi: 220            # Unset fields fall through to global models.vision.* / parsing.vision.*.
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
partition by incremental cache  (skip unchanged files)
      │
      ▼
for each new file:
      ├─ pre_parse hook        DOCX OMML → LaTeX
      ├─ Docling / Media / Text
      ├─ garbled fallback      glyph< → pymupdf
      ├─ Excel denoise         merged cells, sparse rows
      ├─ LibreOffice pages     xlsx/docx/pptx → PDF → screenshots
      ├─ post_parse hook       PPTX chart direct-read
      ├─ Vision enrichment     per-page, 8-layer triage, parallel
      ├─ pre_write hook        exiftool / sanitize
      ├─ Vision dedup          keep the better of Docling / Vision
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
- **PPTX chart direct-read** — python-pptx extracts chart data (categories, series, values) as 100% accurate Markdown tables. Vision supplements with visual context.
- **DOCX math equations** — OMML → LaTeX preprocessing before Docling parses. `$E=mc^{2}$` instead of garbled text.
- **Smart chunking** — auto strategy by format (heading/recursive/slide/sheet/timestamp). CJK-aware token estimation. Protected blocks with per-type overflow control (tables, code, lists) and per-type `on_overflow` strategy — oversized Markdown tables are split at data-row boundaries with the header repeated in every sub-chunk (2026 industry standard, handles Docling's merged-cell expansion). Single-pass heading merge (prelude + orphan-heading + small-section policies) produces zero-fragment chunks with the deepest-available title_path. Adjacent byte-identical chunks auto-deduplicated. All behaviour is config-driven — every knob in `chunking.heading.*` and `chunking.protection.*`.
- **Excel denoising** — merged-cell dedup, sparse row cleanup, embedded image extraction.
- **Content-based format detection** — magika ML model identifies files with weak/missing extensions.
- **Anti-hallucination Vision** — `[?]` for partial reads, `[unreadable]` for gaps. Post-run quality report.
- **Vision triage** — per-page analysis skips pure-text pages, saving 30-60% Vision API cost with zero info loss. Eight-layer defence for damaged pages: `glyph<` / `&lt;` CID markers, U+FFFD ratio, complex-table density, CJK mixed-script anomaly, **language-script consistency** (new) — catches CMap failures that produce CLEAN but WRONG Unicode (e.g. Bengali/Thai/Tibetan chars on a Japanese-declared document; the other checks miss this because the output is legal Unicode). Whitelist per language (ja/zh/en/ko by default), add a language = edit `parsing.vision.triage.language_script_check.expected_scripts` — no code change. Default ON (`parsing.vision.triage.enabled`).
- **Bounding boxes** — per-element PDF coordinates extracted from Docling for RAG source citation and highlighting. Exposed per file in `index.json` (`files[].element_boxes[<page_no>] = [{label, bbox, text_preview}, ...]`); RAG apps look them up by matching `chunk.metadata.source` → index entry. Toggle via `output.include_bounding_boxes`.
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
