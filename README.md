# DocIngest

Universal document preprocessing for RAG and Agentic Search.

Accepts any document (PDF/PPT/Excel/HTML/images/...) → parses with Docling + Vision AI → outputs clean Markdown + chunks + a knowledge map. One preprocessing pipeline, two consumers: **RAG** (vector search on `chunks.jsonl`) and **Agentic Search** (grep/glob on `sources/*.md`).

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
pip install -e .

# Optional: LibreOffice enables Vision enrichment for Excel
winget install TheDocumentFoundation.LibreOffice   # Windows
brew install --cask libreoffice                    # macOS
sudo apt install libreoffice                       # Linux
```

Set API keys. Either export them in your shell, or put them in a `.env`
file at the project root (requires `python-dotenv`, auto-loaded by the CLI):

```
GEMINI_API_KEY=...
# or OPENAI_API_KEY=...
```

## Usage

### 1. Process documents → knowledge base

```bash
docingest run ./docs/ -o ./knowledge/
```

Options:
```
-o, --output PATH    Output directory (default: ./knowledge)
-c, --config PATH    Project config YAML
--strategy TEXT      auto | heading | recursive  (forces top-level chunker;
                     slide/sheet/whole are internal strategies chosen by auto)
--no-chunks          Only output Markdown, skip chunks.jsonl
--parallel INTEGER   Parallel file workers
--force              Ignore cache, full rebuild
```

**Incremental mode is on by default.** Second run skips unchanged files:

```bash
docingest run ./docs/                 # run 1: full pipeline
docingest run ./docs/                 # run 2: 100% cache hit, seconds
docingest run ./docs/ --force         # ignore cache, full rebuild
```

Cache is content-addressed (`md5(head+tail+filename) + size`), so moving files between directories still hits the cache. Invalidates automatically when content changes, config changes, or output files are deleted.

### 2. Refine for human readability (optional)

```bash
docingest refine ./knowledge/sources/spec.md
docingest refine ./knowledge/sources/*.md                   # batch
docingest refine ./knowledge/sources/spec.md --skill my_skill   # custom SKILL
```

Produces `readable/*.md` with:
- Merged duplicate content (Docling + Vision overlap)
- Removed formula residue, HTML comments, openpyxl artifacts
- Flowcharts converted to Mermaid diagrams
- Clean tables and metadata headers

Customizable via `skills/*.SKILL.md` (plain Markdown prompt templates). Original `sources/*.md` is never modified.

### 3. Python API

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

## Configuration

Four layers, highest wins:

```
CLI args (this one run)
  ↓
environment variables (this machine / this deployment)
  ↓
project docingest.yaml (team baseline, checked into git)
  ↓
config/default.yaml (ship defaults)
```

**When to use which:**
- **YAML** — project-wide defaults shared by the team
- **env** — per-machine tuning (e.g. `.env` sets bigger limits on your dev box)
- **CLI** — one-off overrides for a single run (`--force`, `--strategy heading`)

CLI is highest so you can always break out of env settings for ad-hoc runs.

### YAML

```yaml
chunking:
  strategy: "heading"
  max_tokens: 1024

models:
  vision:
    primary: { provider: "google", model: "gemini-3-flash-preview" }
    # swap to any multimodal model: gemini-3-pro-preview,
    # claude-opus-4-6, gpt-5.4, gpt-5.4-mini, ...

parsing:
  vision:
    image_dpi: 180                   # page image resolution for Vision (72/150/180/200)
  xlsx:
    denoising: { enabled: true }     # merged-cell cleanup for layout-heavy Excel
  docx:
    vision_page_images: true         # LibreOffice → PDF → Vision for Word
    max_page_images: 20
  pptx:
    vision_page_images: true         # same for PowerPoint
    max_page_images: 30

incremental:
  enabled: true                      # content-addressed output cache
quality_report:
  enabled: true                      # scan output for [?] / [unreadable] markers
```

### Environment variables (for runtime tuning)

Any config value can be overridden by `DOCINGEST__<path>` env variables.
Use `__` (double underscore) as the nesting separator.

```bash
# Tune chunking on the fly
export DOCINGEST__chunking__max_tokens=1024
export DOCINGEST__chunking__strategy=heading

# Swap Vision model without touching YAML
export DOCINGEST__models__vision__primary__model=gemini-3-pro-preview

# Adjust per-format limits
export DOCINGEST__parsing__docx__max_page_images=50
export DOCINGEST__parsing__xlsx__denoising__max_page_images=20

# Toggle features
export DOCINGEST__incremental__enabled=false
export DOCINGEST__parsing__vision__enabled=false

docingest run ./docs/
```

Values are type-inferred: `true/false` → bool, digits → int, `1.5` → float,
everything else → string. Keys are case-insensitive (Windows-compatible).
Also works via `.env` file at project root.

## Key Features

- **15+ formats** via Docling (PDF, DOCX, PPTX, XLSX, HTML, images, Markdown, ...)
- **Per-page Vision AI** — AI decides per page: clean up text / describe charts / OCR scan. Parallel execution, cached by content hash.
- **Smart chunking** — auto strategy selection by format + structure scoring. CJK-aware token estimation. Protected blocks (tables, code, lists, quotes) kept intact.
- **Excel denoising** — merged-cell dedup, sparse row cleanup, embedded image extraction. Layout-heavy Excel (仕様書) → ~97% noise reduction. Data Excel unaffected.
- **Office Vision fallback** — Excel/Word/PPT all use LibreOffice → PDF → page screenshots → Vision AI for content Docling can't extract (diagrams, screen mockups, margin notes). Configurable per format (`parsing.{xlsx,docx,pptx}.vision_page_images`).
- **Tunable image DPI** — page image resolution (default 180 DPI) balances clarity vs cost. `parsing.vision.image_dpi` controls all Vision render paths uniformly.
- **Anti-hallucination Vision** — prompt enforces `[?]` for partial reads and `[unreadable]` for truly illegible content. AI never invents values. Post-run quality report scans all output for these markers and flags files needing review.
- **Incremental cache** — content-addressed, per-file, crash-safe. 100 specs + 1 new file → only 1 file re-runs.
- **AI Refine** — standalone `refine` command for human-readable output with Mermaid flowcharts, customizable via SKILL files.
- **Knowledge Map** — auto-generated search guide + keyword reverse index + optional AI summary.
- **Multi-provider** — Gemini / OpenAI / Anthropic with automatic fallback (via litellm).
- **Config-driven** — all thresholds, strategies, models in YAML. No hardcoding.

## Project Layout

```
config/default.yaml              # Default configuration
skills/                          # SKILL templates (refine prompts, customizable)
src/docingest/
├── cli.py                       # CLI: run + refine subcommands
├── config.py                    # YAML + env var loader (DOCINGEST__* overrides)
├── pipeline.py                  # Main pipeline (Phase 1.1→1.5, 2, 3, 4) +
│                                #   Excel denoising + Office Vision fallbacks
├── incremental.py               # Content-addressed output cache (cache_key + meta.json)
├── refine.py                    # AI Refine standalone command (sources/ → readable/)
├── parsers/                     # Phase 1: Docling, text fallback, vision (anti-hallucination prompt)
├── chunkers/                    # Phase 3: recursive, heading, slide, sheet, auto router
├── enrichment/                  # path injection ([来源: file > section])
├── models/                      # LLM provider (litellm) + AI call cache (diskcache)
└── output/                      # markdown_writer, chunks_writer, index_builder,
                                 #   knowledge_map, quality_report
test_incremental/                # Regression tests for cache mechanism
DESIGN.md                        # Full design document (architecture, rationale)
```

## Testing

```bash
python tests/test_mixed.py
python tests/test_config_override.py
python test_incremental/run_tests.py   # incremental cache edge cases
```

## Full Design

See [DESIGN.md](DESIGN.md) for architecture details, phase-by-phase rationale, format-specific handling, and benchmark references.
