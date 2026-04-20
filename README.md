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
-o, --output PATH    Output directory (default: ./knowledge)
-c, --config PATH    Project config YAML
--strategy TEXT      auto | heading | recursive  (chunking strategy)
--no-chunks          Only output Markdown, skip chunks.jsonl
--parallel INTEGER   Parallel file workers
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

### Python API

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

Exposes DocIngest as 6 MCP tools: inspect, run, refine, search_knowledge, list_knowledge, read_source.

```bash
pip install -e ".[mcp]"
python -m docingest.mcp_server              # stdio (Claude Desktop / Claude Code)
python -m docingest.mcp_server --transport sse   # SSE (web clients)
```

**Claude Desktop** — add to `claude_desktop_config.json` (Settings > Developer > Edit Config):
```json
{ "mcpServers": { "docingest": { "command": "python", "args": ["-m", "docingest.mcp_server"] } } }
```

**Claude Code** — add to `.mcp.json` at project root (shared) or `~/.claude.json` (personal):
```json
{ "mcpServers": { "docingest": { "type": "stdio", "command": "python", "args": ["-m", "docingest.mcp_server"] } } }
```

**VS Code (Copilot)** — add to `.vscode/mcp.json` (note: key is `servers`, not `mcpServers`):
```json
{ "servers": { "docingest": { "type": "stdio", "command": "python", "args": ["-m", "docingest.mcp_server"] } } }
```

See [MCP_README.md](MCP_README.md) for full tool descriptions, examples, and modification guide.

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
- **Hidden text detection** — flags invisible/background content via Docling ContentLayer analysis.
- **Sensitive data sanitization** — opt-in PII masking (email, URL, credit card with Luhn validation, IPv4, JP phone). High-precision rules only, no name detection. Default OFF (`sanitize.enabled`).
- **Incremental cache** — content-addressed, per-file, crash-safe. 100 docs + 1 new → only 1 re-runs.
- **AI Refine** — standalone `refine` command for human-readable output with Mermaid flowcharts.
- **Knowledge Map** — auto-generated search guide + keyword reverse index. Optional SudachiPy integration for high-precision Japanese keyword extraction (language-routed: Japanese → SudachiPy, Chinese/Korean/English → regex).
- **Multi-provider** — Gemini / OpenAI / Anthropic / DashScope with automatic fallback.
- **Per-format Vision overrides** — `parsing.<pdf|pptx|docx|xlsx>.vision` shallow-merges over the global Vision config to tune `model` / `max_response_tokens` / `image_dpi` per format. Raise DPI for dense PDFs, cap output for content-light PPTs, swap models for scans — without affecting other formats. Unset fields fall through to global.
- **Cross-platform binary finder** — auto-discovers LibreOffice, ffmpeg, yt-dlp, exiftool on Windows/macOS/Linux standard paths.
- **Config-driven** — all thresholds, strategies, models in YAML. No hardcoding.

## Project Layout

```
config/default.yaml              # Default configuration
skills/                          # SKILL templates (refine prompts)
src/docingest/
├── cli.py                       # CLI: run + inspect + refine + doctor
├── config.py                    # YAML + env var loader
├── pipeline.py                  # Main pipeline orchestration
├── incremental.py               # Content-addressed output cache
├── refine.py                    # AI Refine standalone command
├── inspect.py                   # Pre-flight document inspection
├── doctor.py                    # Environment health check
├── mcp_server.py                # MCP Server (6 Agent tools)
├── parsers/                     # Phase 1: document parsing
│   ├── docling_parser.py        # Docling adapter (15+ formats)
│   ├── media_parser.py          # Audio/video (subtitle + ASR)
│   ├── text_parser.py           # Plain text fallback
│   └── vision.py                # Per-page Vision AI
├── chunkers/                    # Phase 3: smart chunking
│   ├── recursive.py / heading.py / slide.py / sheet.py
│   └── timestamp.py             # Audio transcript chunking
├── hooks/                       # Format-specific enrichment
│   ├── pptx_chart.py            # Chart data direct-read
│   ├── docx_omml.py             # OMML → LaTeX
│   ├── file_metadata.py         # Docling origin + exiftool
│   ├── sanitize.py              # PII masking (email/URL/CC/IP/phone)
│   └── _docx_math/              # OMML conversion (ported from MarkItDown)
├── models/                      # LLM provider abstraction
│   ├── provider.py              # Vision + text completion (litellm)
│   ├── audio_provider.py        # ASR (DashScope / litellm)
│   └── cache.py                 # AI call cache (diskcache)
├── utils/                       # Cross-cutting utilities
│   ├── binary_finder.py         # External tool discovery (3-platform)
│   ├── zip_expander.py          # ZIP expansion + bomb protection
│   ├── url_resolver.py          # yt-dlp URL download
│   └── format_detector.py       # magika content detection
├── enrichment/
│   └── path_injector.py         # Chunk source path injection
└── output/
    ├── markdown_writer.py       # Markdown + frontmatter output
    ├── chunks_writer.py         # chunks.jsonl output
    ├── index_builder.py         # index.json generation
    ├── knowledge_map.py         # Knowledge map + SKILL.md
    ├── keyword_extractor.py     # SudachiPy / regex keyword extraction
    └── quality_report.py        # Vision uncertainty scan
```

## Testing

```bash
python tests/unit/test_mixed.py
python tests/unit/test_config_override.py
python tests/incremental/run_tests.py
```

## Full Design

See [DESIGN.md](DESIGN.md) for architecture details and [MARKITDOWN_BORROW.md](MARKITDOWN_BORROW.md) for the MarkItDown reference analysis.
