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

All settings in `config/default.yaml`. Override per project with `docingest.yaml` or CLI args:

```yaml
chunking:
  strategy: "heading"
  max_tokens: 1024

models:
  vision:
    primary: { provider: "google", model: "gemini-3-flash-preview" }

parsing:
  xlsx:
    denoising: { enabled: true }     # merged-cell cleanup for layout-heavy Excel

incremental:
  enabled: true                      # content-addressed output cache
```

## Key Features

- **15+ formats** via Docling (PDF, DOCX, PPTX, XLSX, HTML, images, Markdown, ...)
- **Per-page Vision AI** — AI decides per page: clean up text / describe charts / OCR scan. Parallel execution, cached by content hash.
- **Smart chunking** — auto strategy selection by format + structure scoring. CJK-aware token estimation. Protected blocks (tables, code, lists, quotes) kept intact.
- **Excel denoising** — merged-cell dedup, sparse row cleanup, embedded image extraction. Layout-heavy Excel (仕様書) → ~97% noise reduction. Data Excel unaffected.
- **Excel Vision fallback** — LibreOffice → PDF → page screenshots → Vision AI. Recovers diagrams and margin notes Docling can't extract.
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
├── cli.py                       # CLI: run + refine
├── pipeline.py                  # Main pipeline (Phase 1-4) + Excel denoising
├── incremental.py               # Content-addressed output cache
├── refine.py                    # AI Refine (standalone)
├── parsers/                     # Phase 1: Docling, text, vision
├── chunkers/                    # Phase 3: recursive, heading, slide, sheet
├── enrichment/                  # path injection
├── models/                      # LLM provider + cache
└── output/                      # markdown_writer, chunks_writer, knowledge_map
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
