"""
DocIngest MCP Server — thin wrapper exposing DocIngest as MCP tools.

Architecture:
  This file is a TRANSPORT LAYER ONLY. It converts MCP tool calls into
  DocIngest Python API calls. All business logic lives in the core modules
  (pipeline.py, inspect.py, refine.py, config.py). This file should never
  contain document processing logic.

Adding a new tool:
  1. Add a @mcp.tool function below
  2. Call the corresponding DocIngest Python API
  3. Return structured result (dict/str)
  That's it. No protocol knowledge needed.

Running:
  python -m docingest.mcp_server                    # stdio (default)
  python -m docingest.mcp_server --transport sse    # SSE for web clients

Requires: pip install fastmcp   (or pip install -e ".[mcp]")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Lazy imports — core modules are heavy (Docling, etc.), only load when needed.
# This keeps MCP server startup fast (tool listing doesn't need Docling).

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="DocIngest",
    instructions=(
        "DocIngest preprocesses documents (PDF/PPT/Excel/HTML/images/audio/video/ZIP/URLs) "
        "into Markdown + chunks for RAG and Agentic Search.\n\n"
        "Recommended workflow:\n"
        "1. inspect — Check file sizes and page counts BEFORE processing (fast, no API cost)\n"
        "2. run — Process documents into a knowledge base (incremental: skips unchanged files)\n"
        "3. list_knowledge — Browse what's in the knowledge base (files, formats, sections)\n"
        "4. search_knowledge — Find specific content by keyword (grep on sources/*.md)\n"
        "5. refine — Optional: AI-powered cleanup for human readability\n\n"
        "Tips:\n"
        "- Always inspect first for large/unknown files to estimate Vision API cost\n"
        "- Use config_overrides to dynamically adjust behavior (e.g. increase max_pages for large PDFs)\n"
        "- run is incremental by default — safe to call repeatedly on the same directory\n"
        "- search_knowledge also returns the knowledge_map summary for quick overview"
    ),
)


# ---------------------------------------------------------------------------
# Helper: load config once per process
# ---------------------------------------------------------------------------

_cached_config: dict[str, Any] | None = None


def _get_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load base config (cached) and apply per-call overrides without mutation."""
    global _cached_config
    if _cached_config is None:
        from .config import load_config
        _cached_config = load_config()
    if overrides:
        import copy
        from .config import deep_merge
        return deep_merge(copy.deepcopy(_cached_config), overrides)
    return _cached_config


# ---------------------------------------------------------------------------
# Tool: inspect
# ---------------------------------------------------------------------------

@mcp.tool
def inspect(
    paths: list[str],
    config_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Inspect documents before processing — fast pre-flight check.

    Returns file size, page count, format, and processing recommendations
    WITHOUT actually parsing the documents. Use this to understand what
    you're about to process and estimate cost.

    Args:
        paths: List of file paths or directories to inspect.
        config_overrides: Optional config overrides (e.g. {"parsing": {"vision": {"max_pages": 100}}}).

    Returns:
        List of inspection results, one per file. Each contains:
        name, format, size_mb, pages, recommendation.
    """
    from .inspect import inspect_files

    config = _get_config(config_overrides)
    return inspect_files([Path(p) for p in paths], config)


# ---------------------------------------------------------------------------
# Tool: run
# ---------------------------------------------------------------------------

@mcp.tool
def run(
    paths: list[str],
    output_dir: str = "./knowledge",
    no_chunks: bool = False,
    strategy: str | None = None,
    force: bool = False,
    acknowledge_large: bool = False,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Process documents into a knowledge base (Markdown + chunks + index).

    Converts any document (PDF/PPT/Excel/HTML/images/audio/video/ZIP/URLs)
    into clean Markdown files with metadata, chunked text for RAG, and
    a searchable knowledge map.

    Incremental by default — only processes new/changed files.

    Args:
        paths: List of file paths, directories, or URLs to process.
        output_dir: Output directory (default: ./knowledge).
        no_chunks: If true, only output Markdown, skip chunks.jsonl.
        strategy: Override chunking strategy (auto/heading/recursive/slide/sheet).
        force: Ignore cache, re-process all files.
        acknowledge_large: When safety.mode is "strict" and the pre-run
            budget check flags one or more files, set True to proceed
            anyway. Recommended workflow: call `inspect` first, review
            pages/est_cost_usd, then call `run` with acknowledge_large=True
            if the estimate looks acceptable. Ignored in warn/off modes.
        config_overrides: Optional config overrides dict.

    Returns:
        Processing summary. Keys:
          total_files, successful, failed, total_chunks, total_tokens,
          elapsed_ms, errors, quality.
        Additional keys populated by Phase 0 safety:
          safety — structured report (mode, violations, summary, ...);
                   present when safety ran and produced data.
          status — "aborted_by_safety" when strict mode refused the run;
                   absent otherwise. Agents should inspect safety.violations
                   and retry with acknowledge_large=True if the estimated
                   cost is acceptable.
    """
    from .parsers import create_parser
    from .chunkers import create_chunker
    from .pipeline import run_pipeline

    # Build config with overrides
    overrides: dict[str, Any] = {"output": {"dir": output_dir}}
    if no_chunks:
        overrides.setdefault("chunking", {})["enabled"] = False
    if strategy:
        overrides.setdefault("chunking", {})["strategy"] = strategy
    if force:
        overrides.setdefault("incremental", {})["force"] = True
    if config_overrides:
        from .config import deep_merge
        overrides = deep_merge(overrides, config_overrides)

    config = _get_config(overrides)
    parser = create_parser(config)
    chunker = create_chunker(config) if not no_chunks else None

    result = run_pipeline(
        [Path(p) if not p.startswith(("http://", "https://")) else p for p in paths],
        config,
        parser,
        chunker,
        acknowledge_large=acknowledge_large,
    )

    out: dict[str, Any] = {
        "total_files": result.total_files,
        "successful": result.successful,
        "failed": result.failed,
        "total_chunks": result.total_chunks,
        "total_tokens": result.total_tokens,
        "elapsed_ms": result.elapsed_ms,
        "errors": result.errors,
        "quality": result.quality,
    }
    # Surface Phase 0 safety report so agents can inspect violations and
    # decide whether to retry with acknowledge_large=True. Only included
    # when Phase 0 produced a non-empty dict (keeps typical successful
    # responses compact).
    if getattr(result, "safety", None):
        out["safety"] = result.safety
        if result.safety.get("aborted"):
            out["status"] = "aborted_by_safety"
    return out


# ---------------------------------------------------------------------------
# Tool: refine
# ---------------------------------------------------------------------------

@mcp.tool
def refine(
    files: list[str],
    output_dir: str | None = None,
    skill: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Refine Markdown files for human readability using AI.

    Takes sources/*.md files and produces cleaned, well-formatted
    versions in readable/*.md. Removes noise, merges duplicates,
    improves structure.

    Available skills:
      - "refine_default" (default): readability-first, allows rewriting and polishing
      - "refine_faithful": preserves original wording word-for-word, only deduplicates and reformats

    Args:
        files: List of Markdown file paths to refine.
        output_dir: Output base directory (default: parent of first file).
        skill: SKILL template name. Use "refine_faithful" to preserve original text exactly.
        config_overrides: Optional config overrides dict.

    Returns:
        List of refine results: source, tokens_in, tokens_out, skipped, warning.
    """
    from .refine import refine_files

    config = _get_config(config_overrides)
    file_paths = [Path(f) for f in files]

    if output_dir is None:
        first = file_paths[0].resolve()
        out = first.parent.parent if first.parent.name == "sources" else first.parent
    else:
        out = Path(output_dir)

    return refine_files(file_paths, config, out, skill)


# ---------------------------------------------------------------------------
# Tool: search_knowledge
# ---------------------------------------------------------------------------

@mcp.tool
def search_knowledge(
    query: str,
    knowledge_dir: str = "./knowledge",
    max_results: int = 10,
) -> dict[str, Any]:
    """
    Search a processed knowledge base using keyword matching.

    Greps sources/*.md for the query string and returns matching lines
    with surrounding context. Also loads the knowledge_map summary for
    a quick overview of the knowledge base contents. Lightweight local
    search — no vector database needed.

    Args:
        query: Search query string (case-insensitive).
        knowledge_dir: Path to knowledge base directory.
        max_results: Maximum number of matching lines to return.

    Returns:
        query: The search query.
        matches: list of {file, line, context} dicts.
        total_matches: Number of matches found.
        knowledge_map_summary: Overall knowledge base description (if available).
        stats: File/chunk/token counts (if available).
    """
    import re

    kb_dir = Path(knowledge_dir)
    results: dict[str, Any] = {"query": query, "matches": [], "knowledge_map_summary": None}

    # Load knowledge map summary if available
    km_path = kb_dir / "knowledge_map.yaml"
    if km_path.exists():
        try:
            import yaml
            km = yaml.safe_load(km_path.read_text(encoding="utf-8"))
            results["knowledge_map_summary"] = km.get("summary")
            results["stats"] = km.get("stats")
        except Exception:
            pass

    # Grep sources/*.md for query
    sources_dir = kb_dir / "sources"
    if not sources_dir.exists():
        return results

    matches: list[dict[str, Any]] = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for md_file in sorted(sources_dir.glob("*.md")):
        try:
            lines = md_file.read_text(encoding="utf-8").split("\n")
            for line_no, line in enumerate(lines, 1):
                if pattern.search(line):
                    # Context: line before + match + line after
                    start = max(0, line_no - 2)
                    end = min(len(lines), line_no + 1)
                    context = "\n".join(lines[start:end])
                    matches.append({
                        "file": md_file.name,
                        "line": line_no,
                        "context": context,
                    })
                    if len(matches) >= max_results:
                        break
        except Exception:
            continue
        if len(matches) >= max_results:
            break

    results["matches"] = matches
    results["total_matches"] = len(matches)
    return results


# ---------------------------------------------------------------------------
# Tool: list_knowledge
# ---------------------------------------------------------------------------

@mcp.tool
def list_knowledge(
    knowledge_dir: str = "./knowledge",
) -> dict[str, Any]:
    """
    List contents of a processed knowledge base.

    Returns the full index.json content — all files with their format,
    language, sections, token counts, and chunk counts. Use this to
    understand what's in the knowledge base before searching.

    Args:
        knowledge_dir: Path to knowledge base directory.

    Returns:
        The full index.json content, or error message if not found.
    """
    index_path = Path(knowledge_dir) / "index.json"
    if not index_path.exists():
        return {"error": f"No index.json found at {index_path}. Run 'run' first."}

    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"Failed to read index.json: {e}"}


# ---------------------------------------------------------------------------
# Tool: read_source
# ---------------------------------------------------------------------------

@mcp.tool
def read_source(
    file_name: str,
    knowledge_dir: str = "./knowledge",
    folder: str = "sources",
    max_lines: int | None = None,
) -> dict[str, Any]:
    """
    Read the full content of a Markdown file from the knowledge base.

    Use after search_knowledge or list_knowledge to read a specific file.
    Set folder="readable" to read AI-refined versions (produced by refine tool).

    Args:
        file_name: Name of the file (e.g. "report.md").
        knowledge_dir: Path to knowledge base directory.
        folder: Subfolder to read from — "sources" (default, raw parsed) or "readable" (AI-refined).
        max_lines: Optional line limit (None = full file). Use for very large files.

    Returns:
        file: The file name.
        folder: Which folder was read.
        content: The Markdown content (or truncated if max_lines set).
        total_lines: Total line count of the file.
        truncated: Whether the content was truncated.
    """
    source_path = Path(knowledge_dir) / folder / file_name
    if not source_path.exists():
        return {"error": f"File not found: {source_path}"}

    try:
        text = source_path.read_text(encoding="utf-8")
        lines = text.split("\n")
        total_lines = len(lines)
        truncated = False

        if max_lines is not None and total_lines > max_lines:
            text = "\n".join(lines[:max_lines])
            truncated = True

        return {
            "file": file_name,
            "folder": folder,
            "content": text,
            "total_lines": total_lines,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": f"Failed to read {file_name}: {e}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server."""
    import sys

    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    # Load .env for API keys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
