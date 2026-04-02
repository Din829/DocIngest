"""
Main Pipeline — orchestrates Phase 1 → 2 → 3.

This is the core of DocIngest. It:
  1. Discovers input files
  2. Parses each file (Phase 1) → Markdown in memory
  3. Writes Markdown + assets + index.json (Phase 2)
  4. Chunks the Markdown (Phase 3) → chunks.jsonl

Design:
  - Each file processed independently (error in one doesn't block others)
  - Same in-memory Markdown feeds both Phase 2 and Phase 3 (consistency)
  - Parallel file processing via concurrent.futures (configurable)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_nested
from .parsers.base import BaseParser
from .chunkers.base import BaseChunker
from .chunkers.recursive import RecursiveChunker
from .output.markdown_writer import write_markdown
from .output.index_builder import IndexBuilder
from .output.chunks_writer import write_chunks
from .enrichment.path_injector import inject_paths


# ---------------------------------------------------------------------------
# Pipeline result types
# ---------------------------------------------------------------------------

@dataclass
class FileResult:
    """Result of processing a single file through the full pipeline."""
    original_file: str
    output_path: str = ""       # Path to output .md in sources/
    format: str = ""
    success: bool = True
    error: str = ""
    chunks_count: int = 0
    tokens_estimated: int = 0
    parse_time_ms: int = 0
    chunk_time_ms: int = 0


@dataclass
class PipelineResult:
    """Result of the full pipeline run."""
    files: list[FileResult] = field(default_factory=list)
    total_files: int = 0
    successful: int = 0
    failed: int = 0
    total_chunks: int = 0
    total_tokens: int = 0
    elapsed_ms: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_files(input_paths: list[Path]) -> list[Path]:
    """
    Discover all processable files from input paths.

    Args:
        input_paths: List of file or directory paths.

    Returns:
        Flat list of individual file paths (directories recursively expanded).
    """
    files: list[Path] = []
    for p in input_paths:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            # Recursive scan, skip hidden files/dirs
            for f in sorted(p.rglob("*")):
                if f.is_file() and not any(
                    part.startswith(".") for part in f.parts
                ):
                    files.append(f)
    return files


# ---------------------------------------------------------------------------
# Single file processing
# ---------------------------------------------------------------------------

def process_single_file(
    file_path: Path,
    parser: BaseParser,
    chunker: BaseChunker | None,
    config: dict[str, Any],
    output_dir: Path,
    existing_names: set[str] | None = None,
) -> tuple[FileResult, list]:
    """
    Process a single file through Phase 1 → 2 → 3.

    Args:
        file_path: Input file path.
        parser: Parser instance to use.
        chunker: Chunker instance (None if chunking disabled).
        config: Full config dict.
        output_dir: Base output directory (e.g. ./knowledge/).

    Returns:
        FileResult with processing details.
    """
    result = FileResult(original_file=str(file_path))

    # --- Phase 1: Parse ---
    t0 = time.monotonic()
    try:
        parse_result = parser.parse(file_path)
    except Exception as e:
        result.success = False
        result.error = f"Parse failed: {e}"
        return result, []
    result.parse_time_ms = int((time.monotonic() - t0) * 1000)

    if not parse_result.success:
        # Check error handling config
        on_failure = get_nested(config, "error_handling.on_parse_failure", "skip")
        if on_failure == "fail":
            result.success = False
            result.error = parse_result.error
            return result, []
        # "skip" — mark as failed but continue pipeline for other files
        result.success = False
        result.error = parse_result.error
        return result, []

    result.format = parse_result.metadata.get("format", "unknown")

    # --- Phase 2: Write Markdown + assets ---
    output_path = write_markdown(
        parse_result=parse_result,
        original_file=file_path,
        output_dir=output_dir,
        config=config,
        existing_names=existing_names,
    )
    result.output_path = str(output_path.relative_to(output_dir)).replace("\\", "/")

    # Estimate tokens
    result.tokens_estimated = len(parse_result.markdown) // 4

    # --- Phase 3: Chunk (if enabled) ---
    chunks = []
    if chunker and get_nested(config, "chunking.enabled", True):
        t1 = time.monotonic()

        # Build document metadata for chunker
        doc_metadata = {
            "source": result.output_path,
            "original_file": str(file_path.name),
            "format": result.format,
            **parse_result.metadata,
        }

        try:
            chunks = chunker.chunk(parse_result.markdown, doc_metadata)
        except Exception as e:
            # Chunking failure → fallback behavior from config
            on_failure = get_nested(config, "error_handling.on_chunk_failure", "fallback")
            if on_failure == "fallback":
                try:
                    fallback = RecursiveChunker(config)
                    chunks = fallback.chunk(parse_result.markdown, doc_metadata)
                except Exception:
                    chunks = []
                    result.error = f"Chunk fallback also failed: {e}"
            elif on_failure == "fail":
                result.success = False
                result.error = f"Chunk failed: {e}"
                return result, []
            # "skip" → chunks stays empty, continue

        # Apply enrichment: path injection (if enabled)
        if chunks and get_nested(config, "chunking.enrichment.path_injection", True):
            inject_paths(chunks)

        result.chunks_count = len(chunks)
        result.chunk_time_ms = int((time.monotonic() - t1) * 1000)

    return result, chunks


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _make_index_parse_result(
    file_result: FileResult,
    output_dir: Path,
) -> "ParseResult":
    """
    Create a lightweight ParseResult from FileResult for IndexBuilder.

    Instead of re-reading the written Markdown file, we read it from disk
    (it was just written in Phase 2) to get the content for section extraction.
    """
    from .parsers.base import ParseResult

    md_path = output_dir / file_result.output_path
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except Exception:
        markdown = ""

    return ParseResult(
        markdown=markdown,
        metadata={
            "format": file_result.format,
            "title": Path(file_result.original_file).stem,
        },
    )


def run_pipeline(
    input_paths: list[Path],
    config: dict[str, Any],
    parser: BaseParser,
    chunker: BaseChunker | None = None,
) -> PipelineResult:
    """
    Run the full DocIngest pipeline.

    Args:
        input_paths: Files or directories to process.
        config: Merged configuration dict.
        parser: Parser instance.
        chunker: Chunker instance (None if chunking disabled).

    Returns:
        PipelineResult with details of all processed files.
    """
    t_start = time.monotonic()

    # Resolve output directory
    output_dir = Path(get_nested(config, "output.dir", "./knowledge"))
    output_dir.mkdir(parents=True, exist_ok=True)

    # Discover files
    files = discover_files(input_paths)
    pipeline_result = PipelineResult(total_files=len(files))

    if not files:
        pipeline_result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return pipeline_result

    # Track used output filenames (for dedup across files)
    existing_names: set[str] = set()

    # Index builder for index.json
    index_builder = IndexBuilder(config)

    # Collect all chunks for writing to chunks.jsonl
    all_chunks: list = []

    # Process each file
    for file_path in files:
        file_result, file_chunks = process_single_file(
            file_path=file_path,
            parser=parser,
            chunker=chunker,
            config=config,
            output_dir=output_dir,
            existing_names=existing_names,
        )

        pipeline_result.files.append(file_result)

        if file_result.success:
            pipeline_result.successful += 1
            pipeline_result.total_chunks += file_result.chunks_count
            pipeline_result.total_tokens += file_result.tokens_estimated
            all_chunks.extend(file_chunks)

            # Add to index
            index_builder.add_file(
                parse_result=_make_index_parse_result(file_result, output_dir),
                original_file=file_path,
                output_path=output_dir / file_result.output_path,
                output_dir=output_dir,
                chunks_count=file_result.chunks_count,
            )
        else:
            pipeline_result.failed += 1
            pipeline_result.errors.append({
                "file": file_result.original_file,
                "error": file_result.error,
            })
            index_builder.add_error()

    # Write index.json
    index_builder.write_index(output_dir)

    # Write chunks.jsonl (all chunks from all files)
    if all_chunks and get_nested(config, "chunking.enabled", True):
        write_chunks(all_chunks, output_dir, config)

    # Write errors.json if any failures
    report_file = get_nested(config, "error_handling.report_file", "errors.json")
    if pipeline_result.errors:
        errors_path = output_dir / report_file
        errors_path.write_text(
            json.dumps(pipeline_result.errors, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    pipeline_result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return pipeline_result
