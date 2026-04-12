# DocIngest

Universal document preprocessing for RAG and Agentic Search.

Accepts any document (PDF/PPT/Excel/HTML/images/audio/video/ZIP/URLs/...) → parses with Docling + Vision AI + ASR → outputs clean Markdown + chunks + a knowledge map. One preprocessing pipeline, two consumers: **RAG** (vector search on `chunks.jsonl`) and **Agentic Search** (grep/glob on `sources/*.md`).

## Outputs

| File | Purpose |
|---|---|
| `sources/*.md` | Clean Markdown with frontmatter (Agentic Search, grep/glob) |
| `chunks.jsonl` | Chunked text with metadata (RAG vector search) |
| `index.json` | File directory (Agent file discovery) |
| `knowledge_map.yaml` + `knowledge_search.SKILL.md` | Auto-generated search guide |
| `quality_report.json` | Vision accuracy health check (`[?]` + `[unreadable]` scan) |
| `readable/*.md` | Human-readable version (optional, via `refine`) |

## Install

```bash
cd DocIngest
pip install -e .

# Optional: Japanese keyword extraction (SudachiPy, ~80MB)
pip install -e ".[nlp]"
```

Optional system tools (auto-detected, gracefully skipped if absent):

```bash
# LibreOffice — enables Vision enrichment for Excel/Word/PPT
winget install TheDocumentFoundation.LibreOffice   # Windows
brew install --cask libreoffice                    # macOS
sudo apt install libreoffice                       # Linux

# yt-dlp — enables YouTube/Bilibili/video URL processing
pip install yt-dlp

# ffmpeg — enables video audio extraction + long audio segmentation
winget install Gyan.FFmpeg                         # Windows
brew install ffmpeg                                # macOS

# magika — enables content-based file type detection (optional, ~25MB)
pip install magika
```

Set API keys in `.env` at the project root:

```
GEMINI_API_KEY=...
DASHSCOPE_API_KEY=...       # For Qwen3-ASR (default audio engine)
# OPENAI_API_KEY=...        # Optional fallback for Vision/ASR
```

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
docingest refine ./knowledge/sources/spec.md
docingest refine ./knowledge/sources/*.md
docingest refine ./knowledge/sources/spec.md --skill my_skill
```

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

**Claude Desktop** — add to `claude_desktop_config.json`:
```json
{ "mcpServers": { "docingest": { "command": "python", "args": ["-m", "docingest.mcp_server"] } } }
```

**Claude Code** — add to `.claude/settings.json`:
```json
{ "mcpServers": { "docingest": { "command": "python", "args": ["-m", "docingest.mcp_server"] } } }
```

**VS Code (Copilot / Continue / etc.)** — check your extension's MCP config format, point to the same command.

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

models:
  vision:
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
- **Smart chunking** — auto strategy by format (heading/recursive/slide/sheet/timestamp). CJK-aware token estimation. Protected blocks (tables, code, lists).
- **Excel denoising** — merged-cell dedup, sparse row cleanup, embedded image extraction.
- **Content-based format detection** — magika ML model identifies files with weak/missing extensions.
- **Anti-hallucination Vision** — `[?]` for partial reads, `[unreadable]` for gaps. Post-run quality report.
- **Vision triage** — optional per-page analysis skips pure-text pages, saving 30-60% Vision API cost with zero info loss (`parsing.vision.triage.enabled`).
- **Bounding boxes** — per-element PDF coordinates extracted from Docling for RAG source citation and highlighting.
- **Hidden text detection** — flags invisible/background content via Docling ContentLayer analysis.
- **Sensitive data sanitization** — opt-in PII masking (email, URL, credit card with Luhn validation, IPv4, JP phone). High-precision rules only, no name detection. Default OFF (`sanitize.enabled`).
- **Incremental cache** — content-addressed, per-file, crash-safe. 100 docs + 1 new → only 1 re-runs.
- **AI Refine** — standalone `refine` command for human-readable output with Mermaid flowcharts.
- **Knowledge Map** — auto-generated search guide + keyword reverse index. Optional SudachiPy integration for high-precision Japanese keyword extraction (language-routed: Japanese → SudachiPy, Chinese/Korean/English → regex).
- **Multi-provider** — Gemini / OpenAI / Anthropic / DashScope with automatic fallback.
- **Cross-platform binary finder** — auto-discovers LibreOffice, ffmpeg, yt-dlp, exiftool on Windows/macOS/Linux standard paths.
- **Config-driven** — all thresholds, strategies, models in YAML. No hardcoding.

## Project Layout

```
config/default.yaml              # Default configuration
skills/                          # SKILL templates (refine prompts)
src/docingest/
├── cli.py                       # CLI: run + refine subcommands
├── config.py                    # YAML + env var loader
├── pipeline.py                  # Main pipeline orchestration
├── incremental.py               # Content-addressed output cache
├── refine.py                    # AI Refine standalone command
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
python tests/test_mixed.py
python tests/test_config_override.py
python test_incremental/run_tests.py
```

## Full Design

See [DESIGN.md](DESIGN.md) for architecture details and [MARKITDOWN_BORROW.md](MARKITDOWN_BORROW.md) for the MarkItDown reference analysis.
