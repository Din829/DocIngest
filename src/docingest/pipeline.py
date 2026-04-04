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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_nested
from .parsers.base import BaseParser, PAGEBREAK_MARKER
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
# Language detection (character distribution, no AI, fast)
# ---------------------------------------------------------------------------

def _detect_language(text: str, sample_size: int = 2000) -> str:
    """
    Detect dominant language from character distribution.

    Checks a sample of characters — no external dependencies, no AI calls.
    Returns ISO 639-1 code: "ja", "zh", "ko", "en", or "mixed".
    """
    sample = text[:sample_size]
    cjk = ja_specific = ko_specific = latin = 0

    for ch in sample:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            ja_specific += 1  # Hiragana + Katakana
        elif 0xAC00 <= cp <= 0xD7AF:
            ko_specific += 1  # Hangul
        elif 0x4E00 <= cp <= 0x9FFF:
            cjk += 1  # CJK Unified (shared by ja/zh)
        elif 0x0041 <= cp <= 0x007A:
            latin += 1

    total = ja_specific + ko_specific + cjk + latin
    if total == 0:
        return "unknown"

    # Japanese: has hiragana/katakana
    if ja_specific > total * 0.05:
        return "ja"
    # Korean: has hangul
    if ko_specific > total * 0.05:
        return "ko"
    # Chinese: CJK ideographs but no kana/hangul
    if cjk > total * 0.1:
        return "zh"
    # Latin-dominant
    if latin > total * 0.5:
        return "en"

    return "mixed"


# ---------------------------------------------------------------------------
# Vision enrichment
# ---------------------------------------------------------------------------

def _enrich_with_vision(
    parse_result,
    config: dict[str, Any],
) -> None:
    """
    Per-page Vision enrichment with fallback chain — parallel execution.

    For each page with an image:
      1. Send page image + Docling text to AI (parallel across pages)
      2. If Vision succeeds → append AI result to that page section
      3. If Vision fails → keep Docling text as-is (fallback)

    Code does zero content judgment. AI prompt handles all logic.
    Modifies parse_result.markdown in-place.
    """
    import logging
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .parsers.vision import describe_page_cached
    from .models.cache import AICache

    logger = logging.getLogger(__name__)

    if not parse_result.pages:
        return

    cache_enabled = get_nested(config, "cache.enabled", True)
    cache_dir = get_nested(config, "cache.dir", ".docingest_cache")
    cache = AICache(cache_dir=cache_dir, enabled=cache_enabled)

    # Filter to pages that have images (Vision needs an image)
    vision_tasks = [
        (i, page_data) for i, page_data in enumerate(parse_result.pages)
        if page_data.image_path
    ]
    skipped = len(parse_result.pages) - len(vision_tasks)

    if not vision_tasks:
        logger.info(f"Vision enrichment: 0 pages with images, {skipped} skipped")
        return

    # Parallel Vision calls
    parallel = get_nested(config, "performance.parallel_files", 4)
    results: dict[int, str] = {}  # page_index → vision result text
    described = 0
    failed = 0

    def _call_vision(idx: int, page_data) -> tuple[int, str | None]:
        try:
            return idx, describe_page_cached(
                image_path=page_data.image_path,
                page_text=page_data.text,
                config=config,
                cache=cache,
            )
        except Exception as e:
            logger.warning(f"Vision failed for page {page_data.page_no}: {e}")
            return idx, None

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {
            executor.submit(_call_vision, idx, pd): idx
            for idx, pd in vision_tasks
        }
        for future in as_completed(futures):
            idx, result_text = future.result()
            if result_text and result_text.strip():
                results[idx] = result_text.strip()
                described += 1
            else:
                failed += 1

    # Inject results into markdown sections
    pagebreak = PAGEBREAK_MARKER
    sections = parse_result.markdown.split(pagebreak)

    for idx, text in results.items():
        if idx < len(sections):
            sections[idx] = (
                sections[idx].rstrip()
                + f"\n\n<!-- vision-enriched -->\n{text}\n"
            )

    parse_result.markdown = pagebreak.join(sections)

    if cache_enabled:
        cache.close()

    logger.info(
        f"Vision enrichment: {described} described, {skipped} skipped, {failed} failed (parallel={parallel})"
    )


# ---------------------------------------------------------------------------
# Chunk post-processing
# ---------------------------------------------------------------------------

# Matches any HTML comment (<!-- ... -->), covers image/pagebreak/any placeholder
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _clean_image_noise(text: str) -> str:
    """
    Clean up placeholder noise in chunks. Minimal and safe:
    1. In vision-enriched sections: remove <!-- image --> (Vision already described them)
    2. Elsewhere: collapse consecutive <!-- image --> to a single one
    3. Never touch <!-- pagebreak --> or <!-- vision-enriched -->
    """
    # If this chunk contains vision-enriched content, image placeholders are redundant
    if "<!-- vision-enriched -->" in text:
        text = re.sub(r"\s*<!--\s*image\s*-->\s*", "\n\n", text)

    # Collapse runs of consecutive HTML comments (that aren't structural markers)
    # Structural: pagebreak, vision-enriched — preserve these
    text = re.sub(
        r"(<!--\s*image\s*-->\s*){2,}",
        "<!-- image -->\n\n",
        text,
    )

    # Clean up excessive blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    return text.strip()


def _is_fragment(chunk, min_tokens: int) -> bool:
    """Check if a chunk is a meaningless fragment (tiny or comment-only)."""
    text = chunk.text
    # Strip all HTML comments to see if there's real content
    real_text = _HTML_COMMENT_RE.sub("", text).strip()
    # Remove heading markers to see pure content
    real_text = re.sub(r"^#{1,6}\s+", "", real_text, flags=re.MULTILINE).strip()
    real_tokens = BaseChunker.estimate_tokens(real_text)
    return real_tokens < min_tokens


def _same_section(chunk_a, chunk_b) -> bool:
    """Check if two chunks belong to the same section/sheet/slide."""
    ma, mb = chunk_a.metadata, chunk_b.metadata
    # Compare by the most specific section identifier available
    for key in ("sheet_name", "title_path", "slide_index"):
        va, vb = ma.get(key), mb.get(key)
        if va is not None and vb is not None:
            return va == vb
    # No section identifiers → treat as same section (safe to merge)
    return True


def _postprocess_chunks(
    chunks: list,
    config: dict[str, Any],
) -> list:
    """
    Post-process chunks: clean image noise and merge fragments.

    1. Clean consecutive <!-- image --> placeholders in each chunk
    2. Merge fragment chunks (below min_tokens after cleaning) into neighbors
    """
    if not chunks:
        return chunks

    min_tokens = get_nested(config, "chunking.min_tokens", 100)

    # Step 1: Clean image noise in each chunk
    for chunk in chunks:
        chunk.text = _clean_image_noise(chunk.text)

    # Step 2: Merge fragments into adjacent chunks (respecting section boundaries)
    merged: list = []
    for chunk in chunks:
        if _is_fragment(chunk, min_tokens) and merged:
            # Only merge if same section (sheet_name, title_path, or slide_index)
            if _same_section(merged[-1], chunk):
                merged[-1].text = merged[-1].text + "\n\n" + chunk.text
            else:
                merged.append(chunk)
        else:
            merged.append(chunk)

    # If the first chunk is a fragment and there's a next one in same section, merge forward
    if len(merged) > 1 and _is_fragment(merged[0], min_tokens) and _same_section(merged[0], merged[1]):
        merged[1].text = merged[0].text + "\n\n" + merged[1].text
        merged.pop(0)

    # Re-index + infer content tags from text
    for i, chunk in enumerate(merged):
        chunk.metadata["chunk_index"] = i
        chunk.metadata["total_chunks"] = len(merged)
        chunk.metadata["tokens"] = BaseChunker.estimate_tokens(chunk.text)
        # Infer content flags (useful for RAG filtering)
        chunk.metadata["has_table"] = "|" in chunk.text and "---" in chunk.text
        chunk.metadata["has_image_ref"] = "<!-- image" in chunk.text or "<!-- vision" in chunk.text

    return merged


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

    # --- Phase 1.5: Vision enrichment (describe extracted images) ---
    if parse_result.pages and get_nested(config, "parsing.vision.enabled", True):
        _enrich_with_vision(parse_result, config)

    # Detect language early (so frontmatter and chunks both have it)
    if "language" not in parse_result.metadata:
        parse_result.metadata["language"] = _detect_language(parse_result.markdown)

    # --- Phase 2: Write Markdown + assets ---
    output_path = write_markdown(
        parse_result=parse_result,
        original_file=file_path,
        output_dir=output_dir,
        config=config,
        existing_names=existing_names,
    )
    result.output_path = str(output_path.relative_to(output_dir)).replace("\\", "/")

    # Estimate tokens (CJK-aware)
    result.tokens_estimated = BaseChunker.estimate_tokens(parse_result.markdown)

    # Large file warning (informational, does not block processing)
    md_size_mb = len(parse_result.markdown) / (1024 * 1024)
    max_size = get_nested(config, "output.markdown.max_file_size_mb", 10)
    if md_size_mb > max_size:
        import logging
        logging.getLogger(__name__).warning(
            f"Large document: {file_path.name} → {md_size_mb:.1f}MB Markdown "
            f"(~{result.tokens_estimated:,} tokens). Chunking may be slow."
        )

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

        # Enrich metadata: language detection (if not already set)
        if "language" not in doc_metadata:
            doc_metadata["language"] = _detect_language(parse_result.markdown)

        # Enrich metadata: file modification time
        try:
            mtime = file_path.stat().st_mtime
            import datetime
            doc_metadata["last_modified"] = datetime.datetime.fromtimestamp(
                mtime
            ).isoformat(timespec="seconds")
        except Exception:
            pass

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

        # Post-process: merge fragment chunks and clean image noise
        if chunks:
            chunks = _postprocess_chunks(chunks, config)

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

    # Generate knowledge map (Phase 4)
    if get_nested(config, "knowledge_map.enabled", True):
        from .output.knowledge_map import generate_knowledge_map
        index_file = get_nested(config, "output.index_file", "index.json")
        chunks_file = get_nested(config, "chunking.output_file", "chunks.jsonl")
        try:
            generate_knowledge_map(
                index_path=output_dir / index_file,
                chunks_path=output_dir / chunks_file,
                output_dir=output_dir,
                config=config,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Knowledge map generation failed: {e}")

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
