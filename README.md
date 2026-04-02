# DocIngest

Universal document preprocessing engine for RAG and Agentic Search.

## What it does

Accepts any document (PDF, PPT, Excel, HTML, images, etc.), automatically detects format, extracts content with AI-assisted parsing, and outputs clean Markdown files + optional chunk index — ready for both RAG (vector search) and Agentic Search (grep/glob).

## Architecture

```
Input (any document)
  |
  v
Phase 1: Detect + Parse
  - File type detection (magic bytes / extension)
  - Route to appropriate parser (Docling / fallback)
  - AI: layout analysis, table extraction, OCR (as needed)
  - Output: clean Markdown + metadata
  |
  v
Phase 2: Structured Storage
  knowledge/
  ├── sources/          <- Full Markdown files (for Agentic Search)
  │   ├── report.md
  │   └── proposal.md
  ├── index.json        <- File directory index (for Agent browsing)
  └── assets/           <- Extracted images
  |
  v
Phase 3: Dual-Track Indexing (optional)
  Track A (RAG):     Markdown -> chunking -> embedding -> vector index
  Track B (Agentic): sources/ directory as-is -> grep/glob ready
```

## Key Design Principles

- **Markdown as universal intermediate format** — all documents convert to Markdown first
- **One source, two consumers** — same Markdown serves both RAG and Agentic Search
- **AI assists, not dominates** — program rules handle 80%, AI only intervenes on exceptions
- **Config-driven, not hardcoded** — chunking strategy, AI triggers, output format all configurable
- **Standalone** — independent of DinReAct, usable by any RAG/Agent system

## Status

Early design phase.
