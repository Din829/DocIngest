# DocIngest

Universal document preprocessing engine for RAG and Agentic Search.

## What it does

Accepts any document (PDF, PPT, Excel, HTML, images, etc.), automatically parses with AI-assisted extraction, and outputs:

- **`sources/*.md`** — Clean Markdown files with frontmatter (for Agentic Search: grep/glob)
- **`chunks.jsonl`** — Chunked text with metadata (for RAG: vector search)
- **`index.json`** — File directory index (for Agent file discovery)

Same Markdown serves both RAG and Agentic Search. One source, two consumers.

## Architecture

```
Input: any document (PDF/PPT/Excel/HTML/images/text...)
  │
  ▼
Phase 1: Parse
  Docling (AI layout analysis + TableFormer + OCR)
    → fallback: TextParser (multi-encoding)
  + Vision Model (chart/image description, per-page auto)
  → Output: Markdown (in memory)
  │
  ▼
Phase 2: Output
  knowledge/
  ├── sources/*.md       ← Agentic Search (grep/glob)
  ├── assets/            ← Extracted images
  └── index.json         ← File directory for Agents
  │
  ▼
Phase 3: Smart Chunking
  auto strategy: format detection → structure scoring → best chunker
  ├── heading   (structured docs: split by ## then recursive)
  ├── recursive (unstructured: paragraph → sentence boundaries)
  ├── slide     (PPTX: 1 slide = 1 chunk)
  ├── sheet     (Excel: row groups with header repetition)
  └── whole     (images: 1 file = 1 chunk)
  + Protection rules (tables, code blocks, lists, quotes)
  + Path injection enrichment
  → Output: chunks.jsonl
```

## Quick Start

```bash
# Install dependencies
pip install docling litellm typer pyyaml diskcache rich

# Process documents
python -m docingest.cli ./docs/ -o ./knowledge/

# Or with options
python -m docingest.cli ./docs/ --strategy heading --no-chunks
python -m docingest.cli ./docs/ -c ./my-config.yaml
```

## CLI Options

```
docingest [OPTIONS] INPUTS...

Arguments:
  INPUTS    Files or directories to process

Options:
  -o, --output PATH       Output directory (default: ./knowledge)
  -c, --config PATH       Project config YAML (overrides defaults)
  --no-chunks             Disable chunking (Markdown output only)
  --strategy TEXT         Force: auto, heading, recursive, slide, sheet
  --parallel INTEGER      Parallel file processing count
  --help                  Show help
```

## Output Format

**sources/*.md** (with YAML frontmatter):
```markdown
---
source: report.pdf
format: pdf
title: Annual Report 2025
language: ja
pages: 120
processed_at: 2026-04-02T10:30:00
---

# Content here...
```

**chunks.jsonl** (one JSON per line):
```json
{
  "id": "report_chunk_003",
  "text": "[来源: sources/report.md > Financial Data > Revenue]\nRevenue grew 15%...",
  "metadata": {
    "source": "sources/report.md",
    "original_file": "report.pdf",
    "format": "pdf",
    "title_path": "Financial Data > Revenue",
    "chunk_index": 3,
    "tokens": 487
  }
}
```

## Key Features

| Feature | Detail |
|---------|--------|
| **15+ formats** | PDF, PPTX, XLSX, CSV, HTML, images, Markdown, TXT... (via Docling) |
| **Smart chunking** | Auto strategy selection by format + structure scoring |
| **Protection rules** | Tables, code blocks, lists, quotes kept intact |
| **Path injection** | Every chunk knows its source file + heading path |
| **AI Vision** | Per-page auto: text pages skip Vision, chart pages get described |
| **Multi-provider** | Gemini / OpenAI with automatic fallback |
| **Caching** | AI call results cached by content hash (no duplicate API costs) |
| **Config-driven** | All thresholds, strategies, models configurable via YAML |
| **Error resilient** | One file fails → skip + log, others continue |

## Configuration

All settings in `config/default.yaml`. Override per-project with `docingest.yaml`:

```yaml
# Override just what you need
chunking:
  strategy: "heading"
  max_tokens: 1024

models:
  vision:
    primary:
      provider: "google"
      model: "gemini-3-flash"
```

See [DESIGN.md](DESIGN.md) for full configuration reference.

## Project Structure

```
DocIngest/
├── config/default.yaml          # Default configuration
├── src/docingest/
│   ├── cli.py                   # CLI entry point
│   ├── config.py                # YAML config loading + merge
│   ├── pipeline.py              # Main pipeline orchestration
│   ├── parsers/                 # Phase 1: document parsing
│   │   ├── docling_parser.py    # Docling adapter (15+ formats)
│   │   ├── text_parser.py       # Text/Markdown pass-through
│   │   └── vision.py            # AI image description
│   ├── chunkers/                # Phase 3: smart chunking
│   │   ├── recursive.py         # Default: paragraph → sentence split
│   │   ├── heading.py           # Markdown heading split + recursive
│   │   ├── slide.py             # PPTX: 1 slide = 1 chunk
│   │   └── sheet.py             # Excel: row groups + header repeat
│   ├── enrichment/
│   │   └── path_injector.py     # Source path + title injection
│   ├── models/
│   │   ├── provider.py          # Multi-provider LLM (litellm)
│   │   └── cache.py             # AI call caching
│   └── output/
│       ├── markdown_writer.py   # Markdown + frontmatter output
│       ├── index_builder.py     # index.json generation
│       └── chunks_writer.py     # chunks.jsonl output
├── tests/
│   ├── test_mixed.py            # Content + error + consistency tests
│   └── test_config_override.py  # Config override tests
├── DESIGN.md                    # Full design document
└── knowledge/                   # Default output directory
```

## Running Tests

```bash
python tests/test_mixed.py
python tests/test_config_override.py
```

## Design Reference

Based on 2026 RAG best practices:
- Chunking: [Vecta 2026 Benchmark](https://www.runvecta.com/blog/we-benchmarked-7-chunking-strategies-most-advice-was-wrong) — recursive 512t = 69% (highest)
- Parsing: [Docling](https://github.com/docling-project/docling) — table extraction 97.9%
- Retrieval: [A-RAG](https://arxiv.org/abs/2602.03442) — hierarchical search +5-13%

Full design: [DESIGN.md](DESIGN.md)
