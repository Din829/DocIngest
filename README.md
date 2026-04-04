# DocIngest

Universal document preprocessing engine for RAG and Agentic Search.

## What it does

Accepts any document (PDF, PPT, Excel, HTML, images, etc.), automatically parses with AI-assisted extraction, and outputs:

- **`sources/*.md`** — Clean Markdown files with frontmatter (for Agentic Search: grep/glob)
- **`chunks.jsonl`** — Chunked text with metadata (for RAG: vector search)
- **`index.json`** — File directory index (for Agent file discovery)
- **`knowledge_map.yaml`** — Structured search guide with keywords and file mapping
- **`knowledge_search.SKILL.md`** — Agent-readable search instructions

Same Markdown serves both RAG and Agentic Search. One source, two consumers.

## Architecture

```
Input: any document (PDF/PPT/Excel/HTML/images/text...)
  │
  ▼
Phase 1: Parse
  Docling (AI layout analysis + TableFormer + OCR)
    → fallback: TextParser (multi-encoding)
  → Output: Markdown + per-page data (text + page images)
  │
  ▼
Phase 1.5: Vision Enrichment (per-page, parallel)
  Every page image + Docling text → Vision AI
  AI decides: text complete? → clean up. Has charts? → describe. Scanned? → OCR.
  Fallback: Vision fails → keep Docling text as-is
  → Output: Enriched Markdown (in memory)
  │
  ▼
Phase 2: Output
  knowledge/
  ├── sources/*.md       ← Agentic Search (grep/glob)
  ├── assets/            ← Page images
  └── index.json         ← File directory for Agents
  │
  ▼
Phase 3: Smart Chunking
  auto strategy: format detection → structure scoring → best chunker
  ├── heading   (structured docs: split by ## then recursive)
  ├── recursive (unstructured: paragraph → sentence boundaries)
  ├── slide     (PPTX/PDF slides: pagebreak → 1 page = 1 chunk)
  ├── sheet     (Excel: pagebreak → sheet split → row groups + header repeat)
  └── whole     (images: 1 file = 1 chunk)
  + Protection rules (tables, code blocks, lists, quotes)
  + Fragment merging (section-boundary aware)
  + Path injection enrichment
  + Metadata: language, last_modified, has_table, has_image_ref
  → Output: chunks.jsonl
  │
  ▼
Phase 4: Knowledge Map
  Stage 1 (automatic, zero cost): file index + keyword extraction + reverse index
  Stage 2 (AI, one API call): summary + search strategy guide
  → Output: knowledge_map.yaml + knowledge_search.SKILL.md
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

## Python API

```python
from pathlib import Path
from docingest.config import load_config
from docingest.parsers import create_parser
from docingest.chunkers import create_chunker
from docingest.pipeline import run_pipeline

# Load config (auto-merges default.yaml + project docingest.yaml + overrides)
config = load_config(cli_overrides={
    "output": {"dir": "./knowledge"},
    "parsing": {"vision": {"enabled": True}},
})

# Create parser and chunker
parser = create_parser(config)
chunker = create_chunker(config)

# Run pipeline
result = run_pipeline(
    input_paths=[Path("./docs")],
    config=config,
    parser=parser,
    chunker=chunker,
)

# Result
print(f"Processed: {result.successful}/{result.total_files}")
print(f"Chunks: {result.total_chunks}, Tokens: {result.total_tokens}")

# Output files ready at:
#   knowledge/sources/*.md          → grep/glob
#   knowledge/chunks.jsonl          → vector embedding
#   knowledge/index.json            → file directory
#   knowledge/knowledge_map.yaml    → structured search guide
#   knowledge/knowledge_search.SKILL.md → agent instructions
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

## 経営戦略

Content here...
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
    "language": "ja",
    "chunk_index": 3,
    "tokens": 487,
    "has_table": false,
    "has_image_ref": false,
    "last_modified": "2026-04-02T10:00:00"
  }
}
```

**knowledge_search.SKILL.md** (Agent instructions):
```markdown
# 知識ベース検索

## 概要
AI Agent のツール設計と実装に関する技術文書群...

## 検索ガイド
### 技術概念
- **戦略**: RAG混合検索 → chunks.jsonl
- **例**: Agent とは, MCP の仕組み

### 特定データ
- **戦略**: Agentic Search → grep sources/
- **例**: 商品マスタの価格, Day6の課題
```

## Key Features

| Feature | Detail |
|---------|--------|
| **15+ formats** | PDF, PPTX, XLSX, CSV, HTML, DOCX, images, Markdown, TXT... (via Docling) |
| **Per-page Vision AI** | Every page → AI decides: clean up text / describe charts / OCR scan. Parallel execution |
| **Smart chunking** | Auto strategy selection by format + structure scoring. CJK-aware token estimation |
| **Sheet-aware Excel** | Real sheet names, pagebreak splitting, multi-table detection, header repetition |
| **Slide-aware PPT/PDF** | Pagebreak detection, slide title extraction, per-slide chunking |
| **Section name injection** | Docling group names (sheet/slide/chapter) injected as Markdown headings |
| **Protection rules** | Tables, code blocks, lists, quotes kept intact. Section-boundary-aware merging |
| **Path injection** | Every chunk: `[来源: file > section > subsection]` |
| **Rich metadata** | language, last_modified, has_table, has_image_ref, sheet_name, slide_index |
| **Knowledge Map** | Auto-generated search guide + AI summary + keyword reverse index |
| **Multi-provider** | Gemini / OpenAI with automatic fallback |
| **Caching** | AI call results cached by content hash (no duplicate API costs) |
| **Config-driven** | All thresholds, strategies, models configurable via YAML |
| **Error resilient** | One file fails → skip + log, others continue. Vision fails → Docling text fallback |

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
      model: "gemini-3-flash-preview"

knowledge_map:
  enabled: true
  ai_summary: true    # false → Stage 1 only (zero cost)
```

## Project Structure

```
DocIngest/
├── config/default.yaml          # Default configuration
├── src/docingest/
│   ├── cli.py                   # CLI entry point
│   ├── config.py                # YAML config loading + merge
│   ├── pipeline.py              # Main pipeline orchestration (Phase 1-4)
│   ├── parsers/                 # Phase 1: document parsing
│   │   ├── base.py              # ParseResult + PageData + PAGEBREAK_MARKER
│   │   ├── docling_parser.py    # Docling adapter + section name injection
│   │   ├── text_parser.py       # Text/Markdown pass-through
│   │   └── vision.py            # Per-page Vision AI (prompt-driven, no code judgment)
│   ├── chunkers/                # Phase 3: smart chunking
│   │   ├── base.py              # BaseChunker + CJK-aware token estimation + protection rules
│   │   ├── recursive.py         # Paragraph → sentence split
│   │   ├── heading.py           # Markdown heading split + empty-heading merge
│   │   ├── slide.py             # Pagebreak/HR/heading detection + slide title extraction
│   │   └── sheet.py             # Pagebreak sheet split + multi-table + header repetition
│   ├── enrichment/
│   │   └── path_injector.py     # Source path + title injection
│   ├── models/
│   │   ├── provider.py          # Multi-provider LLM (litellm) with fallback
│   │   └── cache.py             # AI call caching (diskcache + memory)
│   └── output/
│       ├── markdown_writer.py   # Markdown + frontmatter output
│       ├── index_builder.py     # index.json generation
│       ├── chunks_writer.py     # chunks.jsonl output
│       └── knowledge_map.py     # Knowledge map + SKILL.md generation
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
- Vision: Per-page AI enrichment — prompt-driven, zero code judgment
- Knowledge Map: Auto-generated search guide for Agents

Full design: [DESIGN.md](DESIGN.md)
