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
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import get_nested
from .parsers.base import BaseParser, PAGEBREAK_MARKER
from .chunkers.base import BaseChunker
from .chunkers.recursive import RecursiveChunker
from .output.markdown_writer import write_markdown
from .output.index_builder import IndexBuilder
from .output.chunks_writer import write_chunks
from .enrichment.path_injector import inject_paths
from .incremental import (
    compute_cache_key,
    compute_config_hash,
    load_cached_meta,
    save_cached_meta,
    is_cache_valid,
    load_chunks_by_id,
    build_meta,
)
from .hooks import run_pre_parse_hooks, run_post_parse_hooks

import logging
_pipeline_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# External stop channel — thread-safe equivalent of the CLI's Ctrl+C.
# Hosts that run the pipeline on a worker thread (the GUI) can't deliver
# SIGINT, so they call request_stop() instead. Semantics mirror the graceful
# interrupt: between files the run stops and aggregate outputs are written
# for everything completed; within a file, Vision page calls that haven't
# started yet are skipped (the page keeps its Docling text) so a large file
# winds down in seconds instead of minutes. run_pipeline clears the flag on
# entry, so a stop request only ever applies to the run it was issued for.
# ---------------------------------------------------------------------------
_EXTERNAL_STOP = threading.Event()


# ---------------------------------------------------------------------------
# File-level concurrency gates (performance.file_concurrency > 1).
#
# _PARSE_GATE — parsing NEVER runs concurrently, by design (probe-verified):
#   the Docling models are loaded once per parser instance (~1.9GB RSS after
#   one parse), DoclingParser carries per-parse mutable state
#   (_last_parse_metadata), and concurrent parses amplify the intermittent
#   docling-parse Windows std::bad_alloc (probe: a 2nd model copy + a
#   177-page PDF killed the process; see docs/docling_parse_OOM_Windows_
#   长期监控.md). The concurrency win comes from OVERLAP instead: file B
#   parses while file A waits on Vision I/O. The lock sits OUTSIDE
#   run_with_timeout so the parse timeout budget never includes queueing
#   time (and run_with_timeout never leaks a running zombie — measured).
#
# _WRITE_NAMES_GATE — write_markdown mutates the shared `existing_names`
#   collision set; Phase 2 is cheap file I/O, so one coarse lock suffices.
#
# _VISION_GATE — caps TOTAL in-flight Vision calls across all files (each
#   file's own pools stay parallel_files-sized; without a global cap,
#   N files × parallel_files would stampede the provider's rate limit).
#   Sized by run_pipeline from performance.vision_global_concurrency
#   (null → parallel_files, so single-file behaviour is unchanged).
#   Module-level by the same one-run-at-a-time assumption as
#   _EXTERNAL_STOP above.
# ---------------------------------------------------------------------------
_PARSE_GATE = threading.Lock()
_WRITE_NAMES_GATE = threading.Lock()
_VISION_GATE: threading.BoundedSemaphore | None = None


def _vision_permit():
    """Context manager guarding one in-flight Vision call. No-op (nullcontext)
    when the gate is uninitialised — e.g. _enrich_with_vision called outside
    run_pipeline (tests, library callers going through api.refine paths)."""
    from contextlib import nullcontext
    gate = _VISION_GATE
    return gate if gate is not None else nullcontext()


def request_stop() -> None:
    """Ask the currently running pipeline to stop gracefully (thread-safe)."""
    _EXTERNAL_STOP.set()


def stop_requested() -> bool:
    """True when an external stop has been requested for the current run."""
    return _EXTERNAL_STOP.is_set()


class VisionSystemicFailure(Exception):
    """Raised by _enrich_with_vision when EVERY Vision page call failed AND the
    failed pages were ones whose content depended on Vision (scanned/garbled).

    This is the fail-loud half of the single-page-vs-systemic distinction: a
    single page failing keeps its Docling fallback silently (ordinary, handled
    inside the per-page except); a TOTAL failure on content-critical pages must
    not be swallowed, or the run produces a "successful" but empty knowledge
    base. Caught at the Phase-1.5 call site in process_single_file, which turns
    it into a per-file failure (success=False, error_type="vision_systemic_failure")
    so one bad file is reported without crashing the rest of the run."""


def _detect_encrypted_reason(file_path) -> str | None:
    """Thin wrapper around utils.encryption.detect_encrypted — lazy-imported so
    the util loads only when a parse has failed, and fully isolated so a buggy
    detector can never escalate one failed file into a crashed run."""
    try:
        from .utils.encryption import detect_encrypted
        return detect_encrypted(file_path)
    except Exception:
        return None


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
    # Coarse classification of `error` so downstream consumers (Agent / MCP /
    # CI) can branch without grepping the message. Empty string = unclassified
    # (legacy callers that bypass process_single_file stay unaffected).
    # Vocabulary — currently emitted by the pipeline:
    #   ""               unclassified / success
    #   "timeout"        wall-clock cap exceeded (parse / vision)
    #   "parse_error"    parser raised or returned success=False
    #   "io_error"       FileNotFoundError / PermissionError / OSError
    #   "encrypted"      parse failed AND the file is password-protected
    #                    (PDF: definite; OOXML: encrypted-or-corrupt). The
    #                    `error` message is a human-readable, actionable hint.
    #   "vision_systemic_failure"  every Vision page call failed AND the failed
    #                    pages depended on Vision for content (scanned/garbled),
    #                    so the output would be empty/garbled. Controlled by
    #                    parsing.vision.on_systemic_failure (error/warn/ignore).
    # Reserved for future use (consumers may match defensively, but the
    # pipeline does not emit these today):
    #   "chunk_error"    reserved — chunker failures currently surface as parse_error
    #   "interrupted"    reserved — graceful-stop skipped files don't produce an error entry today
    #   "unknown"        reserved catch-all
    error_type: str = ""
    chunks_count: int = 0
    tokens_estimated: int = 0
    parse_time_ms: int = 0
    chunk_time_ms: int = 0
    # Per-element PDF coordinates from Docling — page_no -> list of
    # {label, bbox, text_preview}. Piped through FileResult so it reaches
    # index_builder without going through the sources/*.md frontmatter
    # (which only holds small scalars). None when Docling didn't emit any.
    element_boxes: dict[str, Any] | None = None
    # Per-page PDF dimensions (points) — page_no -> [width, height]. The
    # sibling of element_boxes (both set together under
    # include_bounding_boxes). Piped through FileResult the same way so it
    # reaches index.json, where the visualizer reads it to scale bboxes onto
    # rendered page images by true page size instead of an assumed DPI.
    # None when Docling didn't emit any.
    page_sizes: dict[str, Any] | None = None
    # Lifecycle status for this run — consumed by run_log to render a
    # human-readable timeline. One of: "added" (new file, first time seen),
    # "updated" (cache existed but invalidated), "cached" (hit, reused),
    # "forced" (full rebuild via --force), "failed" (parse/chunk error).
    # Empty string means the pipeline did not tag it (legacy / unit-test
    # callers that bypass run_pipeline stay unaffected).
    status: str = ""
    # When status == "updated", why the cache was invalidated
    # (e.g. "config changed", "source markdown missing"). Free-form string
    # surfaced from incremental.is_cache_valid for diagnostic logging.
    cache_reason: str = ""
    # Per-file non-fatal warnings — collected from parse_result.metadata["warnings"]
    # (which hooks/phases populate during processing). Distinct from `error`
    # in that they don't fail the file; instead they signal a quality /
    # completeness compromise (page cap hit, OCR engine downgraded, ...).
    # run_pipeline aggregates these into PipelineResult.warnings.
    warnings: list[str] = field(default_factory=list)
    # Per-file Vision triage tally — collected from
    # parse_result.metadata["vision_triage"] (set by _enrich_with_vision).
    # run_pipeline sums these into PipelineResult.vision_triage. Empty for
    # cached / non-paged / Vision-disabled files. Keys mirror that dict.
    vision_triage: dict[str, int] = field(default_factory=dict)


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
    # True when the run was halted by SIGINT (Ctrl+C) between files.
    # Aggregate outputs (chunks.jsonl, index.json, knowledge_map) still
    # write the partial result for already-processed files. False on a
    # normal completion or on a forced exit (Ctrl+C×2, which propagates
    # KeyboardInterrupt out of the pipeline).
    interrupted: bool = False
    # Quality report summary (populated after Phase 4 if enabled).
    # Keys: total_files, files_with_issues, total_questions, total_unreadable,
    #       quality_score, files (only files with issues).
    quality: dict[str, Any] = field(default_factory=dict)
    # LLM API token usage summary (populated at pipeline end).
    # Keys: total_prompt_tokens, total_completion_tokens, total_tokens,
    #       total_calls, total_cache_hits, by_model.
    token_usage: dict[str, Any] = field(default_factory=dict)
    # Corpus-wide Vision triage tally (summed across all processed files), so a
    # run report shows at a glance how Vision cost broke down: how many pages
    # carried pictures, how many actually went to Vision, how many were skipped
    # by triage, and how many of those skips were furniture (logo/header) — the
    # last is also the savings figure for parsing.vision.triage.furniture_exempt.
    # Keys: pages_with_pictures, sent_to_vision, triage_skipped,
    #       furniture_skipped, cap_skipped, no_image_pages. Empty when Vision
    # is disabled or no paged files were processed.
    vision_triage: dict[str, int] = field(default_factory=dict)
    # Phase 0 safety check result (populated when safety.enabled and mode
    # != "off"). Keys: mode, violations (list of {file, reasons}), summary
    # ({total_files, total_pages, total_est_cost_usd}), acknowledged (bool),
    # aborted (bool — only true when strict mode refused to run).
    # Empty dict when safety is off or no violations occurred.
    safety: dict[str, Any] = field(default_factory=dict)
    # Aggregated non-fatal warnings — situations where the file was processed
    # successfully but a quality / completeness compromise was made (e.g. page
    # images capped, OCR engine downgraded, fallback parser used). Distinct
    # from `errors` (which mark a file as failed) — every entry here lives on
    # a successful file. Each entry: {"file": str, "kind": str, "message": str}.
    # Surfaced to callers via result.stats["warnings"] so a `result.successful
    # == N, warnings == 0` invariant means "everything completed cleanly".
    warnings: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

@dataclass
class InvalidInput:
    """An input path/URL that could not be resolved to a real file.

    Surfaced through discover_files (and from there into PipelineResult.errors
    via run_pipeline) so cross-container / typo / wrong-mount scenarios fail
    loud instead of silently producing zero results.

    Reasons currently emitted:
      "not_found"      — local path does not exist on this filesystem
      "url_failed"     — URL resolution returned no media (yt-dlp / HTTP miss)
      "url_disabled"   — URL input given but parsing.url.enabled=false

    Future reasons may be added; consumers should branch on the string
    rather than enumerate.
    """
    input: str       # the original path / URL the caller passed
    reason: str      # short stable token (see docstring)
    detail: str = ""  # optional human-readable context


def discover_files(
    input_paths: list[Path | str],
    config: dict[str, Any] | None = None,
) -> tuple[list[Path], list[InvalidInput]]:
    """
    Discover all processable files from input paths.

    Accepts a mix of:
      * Local file/directory paths (Path objects or strings)
      * HTTP/HTTPS URLs (strings — YouTube, Bilibili, direct media, etc.)

    Directories are expanded recursively (hidden dirs/files skipped).
    URLs are resolved via yt-dlp (audio + subtitles + metadata downloaded
    to a persistent cache directory).
    Zip archives are expanded into their contents.

    Args:
        input_paths: List of file paths, directory paths, or URL strings.
            Path objects that look like URLs (https://...) are detected
            and handled as URLs, but passing URLs as plain strings is
            preferred because Path() mangles URL slashes on Windows.
        config: Full DocIngest config dict. When None, zip/URL expansion
            is disabled.

    Returns:
        Tuple ``(valid_files, invalid_inputs)``:

          * ``valid_files``    — flat list of real file paths ready for
            processing (directories + ZIPs + URLs expanded).
          * ``invalid_inputs`` — list of InvalidInput describing inputs that
            could not be resolved (missing files, URL resolution failures,
            URL given while parsing.url.enabled=false). Each entry has
            ``input`` (the caller's original string) and ``reason`` (a
            short stable token). Callers should surface these — silently
            dropping invalid paths is how the cross-container "successful=0
            with no errors" failure mode happens.

    Note for callers:
        Returning invalid inputs alongside valid ones (instead of raising)
        lets one bad path through a 100-file batch fail gracefully without
        nuking the rest. run_pipeline transforms each InvalidInput into a
        FileResult(success=False, error_type="io_error") so it appears in
        the standard ``result.stats["errors"]`` channel.
    """
    # First pass: separate URLs from filesystem paths. We work with raw
    # strings first because Path("https://...") on Windows mangles the
    # URL (double slash → single slash), making URL detection unreliable
    # if done after Path conversion.
    raw_files: list[Path] = []
    url_inputs: list[str] = []
    invalid: list[InvalidInput] = []

    for p in input_paths:
        # Preserve the original string form for URL detection.
        # If the caller passed a string, use it directly. If they passed
        # a Path, str() on Windows gives "https:\..." — so we check the
        # original type to decide how to detect URLs.
        original_str = p if isinstance(p, str) else str(p)
        # On Windows, Path("https://x") → "https:\\x" via str(), but
        # Path("https://x").as_posix() → "https:/x" (single slash).
        # Neither preserves the original "https://". So we also check
        # for the Windows-mangled form.
        is_url = (
            original_str.startswith("http://")
            or original_str.startswith("https://")
            or original_str.startswith("http:\\")
            or original_str.startswith("https:\\")
        )
        if is_url:
            # Normalize the URL back to proper form (undo Windows mangling)
            url = original_str.replace("\\", "/")
            # Fix double-slash that Path may have collapsed
            if "://" not in url:
                url = url.replace(":/", "://", 1)
            url_inputs.append(url)
        else:
            path = Path(p) if isinstance(p, str) else p
            if path.is_file():
                raw_files.append(path)
            elif path.is_dir():
                # Recursive scan, skip hidden files/dirs
                for f in sorted(path.rglob("*")):
                    if f.is_file() and not any(
                        part.startswith(".") for part in f.parts
                    ):
                        raw_files.append(f)
            else:
                # Neither a file nor a directory — almost certainly the
                # caller's mistake. Most common cause in production: a
                # cross-container handoff where API writes /tmp and worker
                # reads /tmp, but the two filesystems are different
                # (Container Apps pods, K8s pods, separate Docker containers).
                # Surfacing this as a structured "io_error" entry in
                # result.errors prevents the silent "successful=0" failure
                # mode that's hard to diagnose without trace-level logging.
                invalid.append(InvalidInput(
                    input=str(p),
                    reason="not_found",
                    detail=(
                        "path is not a file or directory in this process's "
                        "filesystem (cross-container handoff? wrong mount? "
                        "typo?)"
                    ),
                ))
                _pipeline_logger.warning(
                    "Input path not found: %s — see InvalidInput / "
                    "result.errors entry with reason='not_found'", p
                )

    # Resolve URL inputs → local files (audio + subtitles + metadata).
    # Only when config is available and URL parsing is enabled.
    url_enabled = config is None or get_nested(config, "parsing.url.enabled", True)
    if url_inputs:
        if not url_enabled:
            for url in url_inputs:
                invalid.append(InvalidInput(
                    input=url,
                    reason="url_disabled",
                    detail="parsing.url.enabled is false",
                ))
        elif config is not None:
            from .utils.url_resolver import resolve_url
            for url in url_inputs:
                resolved = resolve_url(url, config)
                if resolved:
                    raw_files.extend(resolved)
                    _pipeline_logger.info(
                        f"URL resolved: {url} → {len(resolved)} file(s)"
                    )
                else:
                    invalid.append(InvalidInput(
                        input=url,
                        reason="url_failed",
                        detail="resolver returned no media (yt-dlp / HTTP)",
                    ))
                    _pipeline_logger.warning(f"URL resolution failed: {url}")

    # Second pass: expand zip archives when enabled. We run this after
    # the directory walk so zip files found INSIDE an input directory
    # are also expanded, not just zips passed directly on the command
    # line.
    if config is None or not get_nested(config, "parsing.zip.enabled", True):
        return raw_files, invalid

    # Lazy import to avoid a hard dependency from non-zip callers and to
    # keep the top of pipeline.py uncluttered.
    from .utils.zip_expander import should_expand, expand_zip, get_extract_root

    extract_root = get_extract_root(config)
    expanded: list[Path] = []

    for f in raw_files:
        if should_expand(f):
            extract_root.mkdir(parents=True, exist_ok=True)
            inner_files = expand_zip(f, extract_root, config)
            if inner_files is not None:
                expanded.extend(inner_files)
                _pipeline_logger.info(
                    f"Zip expansion: {f.name} → {len(inner_files)} file(s)"
                )
            else:
                # Expansion failed or was refused (corrupt, bomb, password).
                # Keep the original zip in the list so the pipeline surfaces
                # the error explicitly through the normal parse-failure path.
                expanded.append(f)
        else:
            expanded.append(f)

    return expanded, invalid


# ---------------------------------------------------------------------------
# Language detection (character distribution, no AI, fast)
# ---------------------------------------------------------------------------

def _detect_language(text: str, sample_size: int = 2000) -> str:
    """
    Detect dominant language from character distribution.

    Checks a sample of characters — no external dependencies, no AI calls.
    Returns ISO 639-1 code: "ja", "zh", "ko", "en", "mixed", or "unknown".

    Denominator note: `significant_chars` counts EVERY non-whitespace code
    point in the sample, not just CJK/kana/hangul/Latin. An earlier revision
    divided by the sum of the four buckets, which made garbled PDFs
    (Bengali/Thai/Tibetan Unicode from CMap failure, plus a handful of
    Latin chars) misdetect as "en" because the few Latin chars dominated
    an artificially small denominator. Counting all significant chars
    means garble correctly falls through to "unknown" / "mixed".
    """
    sample = text[:sample_size]
    cjk = ja_specific = ko_specific = latin = 0
    significant_chars = 0

    for ch in sample:
        if ch.isspace():
            continue
        significant_chars += 1
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            ja_specific += 1  # Hiragana + Katakana
        elif 0xAC00 <= cp <= 0xD7AF:
            ko_specific += 1  # Hangul
        elif 0x4E00 <= cp <= 0x9FFF:
            cjk += 1  # CJK Unified (shared by ja/zh)
        elif 0x0041 <= cp <= 0x007A:
            latin += 1
        # Everything else (Bengali / Thai / Tibetan / Cyrillic / ...) gets
        # counted into significant_chars but into no bucket, so garbled text
        # drives EVERY bucket ratio toward zero → falls through to unknown.

    if significant_chars == 0:
        return "unknown"

    # Japanese: has hiragana/katakana
    if ja_specific > significant_chars * 0.05:
        return "ja"
    # Korean: has hangul
    if ko_specific > significant_chars * 0.05:
        return "ko"
    # Chinese: CJK ideographs but no kana/hangul
    if cjk > significant_chars * 0.1:
        return "zh"
    # Latin-dominant
    if latin > significant_chars * 0.5:
        return "en"
    # None of the buckets dominate — could be mixed multilingual content or
    # garbled Unicode. Neither is safe to label; "unknown" is the honest answer.
    return "unknown"


# ---------------------------------------------------------------------------
# Garbled text detection + pymupdf fallback
# ---------------------------------------------------------------------------

def _detect_garbled(markdown: str, threshold: int = 10) -> bool:
    """
    Detect garbled output from Docling (broken CID-to-Unicode mapping).

    Checks for 'glyph<c=' patterns which indicate font encoding failures.
    Returns True if garbled text count exceeds threshold.
    """
    count = markdown.count("glyph<") + markdown.count("glyph&lt;")
    return count >= threshold


def _pymupdf_fallback(file_path: Path, original_parse_result) -> None:
    """
    Re-extract text using pymupdf (fitz) when Docling produces garbled output.

    Replaces parse_result.markdown in-place. Preserves pages/metadata from Docling
    (images, page count etc.) — only replaces the text content.

    Minimal invasion: only called when garbled text is detected.
    """
    try:
        import fitz
    except ImportError:
        _pipeline_logger.warning("pymupdf not installed, cannot fallback. pip install pymupdf")
        return

    try:
        doc = fitz.open(str(file_path))
        pages_text = []
        for page in doc:
            pages_text.append(page.get_text())
        doc.close()

        # Rebuild markdown from pymupdf text
        # Use PAGEBREAK_MARKER between pages to maintain page structure
        markdown_parts = []
        for i, text in enumerate(pages_text):
            # Clean up and convert to basic markdown
            lines = text.strip().split("\n")
            cleaned = "\n".join(line.strip() for line in lines if line.strip())
            markdown_parts.append(cleaned)

        new_markdown = f"\n{PAGEBREAK_MARKER}\n".join(markdown_parts)

        # Also update page text data (for Vision enrichment)
        if original_parse_result.pages:
            for i, page_data in enumerate(original_parse_result.pages):
                if i < len(pages_text):
                    page_data.text = pages_text[i]

        original_parse_result.markdown = new_markdown
        _pipeline_logger.info(
            f"pymupdf fallback: replaced garbled Docling output with pymupdf text "
            f"({len(pages_text)} pages)"
        )
    except Exception as e:
        _pipeline_logger.warning(f"pymupdf fallback failed: {e}")


# ---------------------------------------------------------------------------
# Excel denoising (applied to ALL xlsx/xls — unified path)
# ---------------------------------------------------------------------------

def _dedup_table_row(line: str) -> str:
    """
    Collapse merged-cell noise within a single Markdown table row.

    Two strategies (both safe):

    1. ALL cells identical → collapse to 1 cell
       '| foo | foo | foo |' → '| foo |'

    2. GROUPS of consecutive identical cells → each group collapses to 1
       '| A | A | A | B | B | B | C | C |' → '| A | B | C |'
       This handles multi-column merged cells (e.g., header spans 5 cols,
       data spans 20 cols — each column's merge expands independently).

    Safety: only collapses when the dedup removes ≥50% of cells.
    A row like '| a | a | b |' (3 cells → 2, only 33% reduction) is kept.
    This prevents accidentally merging legitimate adjacent duplicate values
    in small tables while aggressively cleaning layout-heavy noise.
    """
    if not line.strip().startswith("|"):
        return line
    cells = [c.strip() for c in line.split("|")]
    cells = [c for c in cells if c]
    if len(cells) <= 2:
        return line

    # Run-length dedup: collapse consecutive identical cells
    deduped: list[str] = [cells[0]]
    for c in cells[1:]:
        if c != deduped[-1]:
            deduped.append(c)

    # Safety check: only apply if we removed ≥50% of cells
    # (strong signal of merged-cell noise, not coincidental duplicates)
    if len(deduped) < len(cells) and len(deduped) <= len(cells) * 0.5:
        return "| " + " | ".join(deduped) + " |"
    return line


def _strip_empty_cells(line: str) -> str:
    """
    Remove empty cells from a Markdown table row, but only when the row
    is *predominantly* empty (sparse layout noise, not a data table).

    Safe:   '| | | 画面ID | | A15S010 | | |'  → '| 画面ID | A15S010 |'
            (2 values in 7 cells = 71% empty → strip)
    Safe:   '| 商品名 | | 価格 |'              → untouched
            (2 values in 3 cells = 33% empty → keep, might be intentional)
    Safe:   '| a | b | c | d |'                 → untouched (no empties)

    Threshold: only strip when >50% of cells are empty.
    This preserves data tables with occasional blank columns while cleaning
    layout-heavy Excel noise (where most cells are empty spacers).

    Entirely empty rows are always removed (zero information).
    """
    if not line.strip().startswith("|"):
        return line
    # Don't touch separator rows
    if re.match(r"^\s*\|[-:\s|]+\|\s*$", line):
        return line
    cells = [c.strip() for c in line.split("|")]
    cells = [c for c in cells if c is not None]  # keep bookend-stripped list
    # Filter: treat "None" (Docling artifact) same as empty
    non_empty = [c for c in cells if c and c.lower() != "none"]
    if not non_empty:
        return ""  # entire row is empty → remove
    # Only strip empty cells when the row is predominantly empty (>50%)
    empty_ratio = 1 - len(non_empty) / max(len(cells), 1)
    if empty_ratio > 0.5:
        return "| " + " | ".join(non_empty) + " |"
    return line


def _extract_metadata_kv(lines: list[str], max_rows: int) -> dict[str, str]:
    """
    Try to extract key-value metadata from the first N rows of Excel output.

    Recognises patterns like:
      '| 画面ID | A15S010 |'  → {"画面ID": "A15S010"}
      '| 作成者 | | 山田 |'  → {"作成者": "山田"}

    Only extracts rows that look like label-value pairs (2 non-empty cells).
    Data table headers (3+ non-empty cells) are left alone.
    """
    kv: dict[str, str] = {}
    for line in lines[:max_rows]:
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c and c.lower() != "none"]
        if len(cells) == 2:
            kv[cells[0]] = cells[1]
    return kv


def _denoise_markdown_table_rows(markdown: str) -> str:
    """
    Apply row-level merged-cell dedup to every Markdown table line.

    Lightweight companion to _clean_excel_markdown: it reuses the same
    _dedup_table_row routine (run-length collapse with a ≥50% safety gate)
    but skips the Excel-specific metadata extraction / empty-cell stripping.
    Appropriate for PDF / DOCX / PPTX / HTML where Docling's Markdown still
    has the merged-cell artefact but the rest of the markdown should stay
    untouched.

    Safe on non-table input — non-`|` lines pass through unchanged.
    """
    if not markdown or "|" not in markdown:
        return markdown
    return "\n".join(_dedup_table_row(line) for line in markdown.split("\n"))


def _clean_excel_markdown(
    markdown: str,
    config: dict[str, Any],
) -> str:
    """
    Denoise Docling's Markdown output for Excel files.

    Applied to ALL Excel files uniformly:
      - Data-heavy spreadsheets have little noise → minimal change
      - Layout-heavy spreadsheets get significant cleanup

    Three passes:
      1. dedup_cells:  collapse identical consecutive cells in each row
      2. strip_empty:  remove empty cells from table rows, drop empty rows
      3. metadata:     extract key-value pairs from first N rows into metadata

    Returns cleaned markdown. Metadata extraction is best-effort (non-destructive).

    Sheet boundaries (PAGEBREAK_MARKER) are preserved — cleaning is done
    per-section so downstream Vision injection / dedup / chunking stay aligned
    to sheets. Without this guard, multi-sheet xlsx files lose pagebreaks
    here and a later Vision-dedup pass can silently drop entire sheets.
    """
    # Preserve sheet boundaries: clean each section independently, then rejoin.
    if PAGEBREAK_MARKER in markdown:
        parts = markdown.split(PAGEBREAK_MARKER)
        return PAGEBREAK_MARKER.join(
            _clean_excel_markdown(p, config) for p in parts
        )

    xlsx_denoise = get_nested(config, "parsing.xlsx.denoising", {})
    if not xlsx_denoise.get("enabled", True):
        return markdown

    do_dedup = xlsx_denoise.get("dedup_cells", True)
    do_strip = xlsx_denoise.get("strip_empty_cells", True)

    lines = markdown.split("\n")
    cleaned: list[str] = []

    for line in lines:
        if do_dedup:
            line = _dedup_table_row(line)
        if do_strip:
            line = _strip_empty_cells(line)
        # _strip_empty_cells returns "" for all-empty rows → skip them
        if line == "":
            cleaned.append("")
        else:
            cleaned.append(line)

    # Pass 3: Inter-row dedup — collapse consecutive identical table rows,
    # but ONLY for single-cell rows (the merged-cell noise signature).
    # Multi-column rows with identical content are legitimate data
    # (e.g., repeated status values across records) — never collapsed.
    if do_dedup:
        deduped: list[str] = []
        for line in cleaned:
            stripped = line.strip()
            is_table = stripped.startswith("|")
            is_separator = is_table and re.match(r"^\s*\|[-:\s|]+\|\s*$", stripped)
            if is_table and not is_separator and deduped:
                # Count non-empty cells — only dedup single-value rows
                cells = [c.strip() for c in stripped.split("|") if c.strip()]
                prev_stripped = deduped[-1].strip()
                if len(cells) <= 1 and prev_stripped == stripped:
                    continue  # skip: single-cell duplicate (merged-cell noise)
            deduped.append(line)
        cleaned = deduped

    result = "\n".join(cleaned)
    # Collapse excessive blank lines created by removed rows
    result = re.sub(r"\n{4,}", "\n\n\n", result)

    original_len = len(markdown)
    new_len = len(result)
    if original_len > 0 and new_len < original_len * 0.8:
        _pipeline_logger.info(
            f"Excel denoising: {original_len:,} → {new_len:,} chars "
            f"({(1 - new_len / original_len) * 100:.0f}% reduction)"
        )

    return result


def _downscale_image(image_path: str, max_pixels: int) -> None:
    """
    Downscale an image file in-place if it exceeds max_pixels.

    Preserves aspect ratio. Uses Lanczos resampling for quality.
    Does nothing if the image is already within limits.
    """
    try:
        from PIL import Image
        img = Image.open(image_path)
        w, h = img.size
        pixels = w * h
        if pixels <= max_pixels:
            return
        scale = (max_pixels / pixels) ** 0.5
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(image_path)
        _pipeline_logger.info(
            f"Downscaled image: {w}x{h} → {new_w}x{new_h} ({image_path})"
        )
    except Exception as e:
        _pipeline_logger.debug(f"Image downscale failed: {e}")


# Legacy binary Office formats → their modern OOXML equivalents via
# LibreOffice. Docling only accepts the OOXML forms (.docx/.pptx/.xlsx); the
# pre-2007 BIFF/CFB forms (.xls/.doc/.ppt) must be converted first, after which
# every downstream Phase (parser / page-image / chunking) flows through the
# modern-format path unchanged. Map: legacy suffix → (target ext, config key).
_LEGACY_OFFICE_CONVERSIONS: dict[str, tuple[str, str]] = {
    ".xls": ("xlsx", "parsing.xls.auto_convert_to_xlsx"),
    ".doc": ("docx", "parsing.doc.auto_convert_to_docx"),
    ".ppt": ("pptx", "parsing.ppt.auto_convert_to_pptx"),
}


def _maybe_convert_legacy_office(
    file_path: Path,
    config: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, dict[str, Any] | None]:
    """
    Legacy binary Office (.xls / .doc / .ppt) → modern OOXML (.xlsx / .docx /
    .pptx) via LibreOffice, so the rest of the pipeline can use the full modern
    path (Docling / openpyxl renderer + Vision + chunking). Docling rejects the
    pre-2007 binary forms outright, so without this they fail at parse.

    Returns (effective_path, transformation_record):
      - non-legacy suffix or disabled → (file_path, None) untouched
      - LibreOffice missing  → (file_path, None) + warning, caller falls
                                through to TextParser
      - conversion failure   → (file_path, None) + warning, same fallback
      - success              → (cached_modern_path, {"step": "format_convert", ...})

    Cache: {output_dir}/.cache/_legacy_convert/<sha256_of_original>.<target>
    Keyed on the original bytes so the same file is converted exactly once per
    output directory across runs.
    """
    conversion = _LEGACY_OFFICE_CONVERSIONS.get(file_path.suffix.lower())
    if conversion is None:
        return file_path, None
    target_ext, config_key = conversion
    if not get_nested(config, config_key, True):
        return file_path, None

    src_suffix = file_path.suffix.lower()  # e.g. ".xls"

    import hashlib
    import shutil
    import subprocess
    import tempfile
    from .utils.binary_finder import find_binary, run_soffice_convert

    soffice = find_binary("soffice", config)
    if not soffice:
        _pipeline_logger.warning(
            f"{src_suffix} input {file_path.name}: LibreOffice not found, "
            f"cannot convert to .{target_ext} — will fall through to "
            f"TextParser (likely gibberish). Install LibreOffice or set "
            f"{config_key}: false to silence this."
        )
        return file_path, None

    try:
        # Stream-hash the source to avoid loading huge files into memory
        h = hashlib.sha256()
        with file_path.open("rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        digest = h.hexdigest()
    except OSError as e:
        _pipeline_logger.warning(
            f"{src_suffix} input {file_path.name}: cannot read for hashing "
            f"({e}) — skipping conversion"
        )
        return file_path, None

    cache_dir = output_dir / ".cache" / "_legacy_convert"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_modern = cache_dir / f"{digest}.{target_ext}"

    if not cached_modern.exists():
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                proc = run_soffice_convert(
                    file_path, tmpdir, target_ext, config=config, timeout=180,
                )
                produced = list(Path(tmpdir).glob(f"*.{target_ext}"))
                if proc is None:
                    # soffice unavailable — same degrade as a failed convert.
                    _pipeline_logger.warning(
                        f"{src_suffix} → .{target_ext} conversion skipped for "
                        f"{file_path.name}: LibreOffice not found"
                    )
                    return file_path, None
                if proc.returncode != 0 or not produced:
                    _pipeline_logger.warning(
                        f"{src_suffix} → .{target_ext} conversion failed for "
                        f"{file_path.name} (exit={proc.returncode}): "
                        f"{proc.stderr.decode('utf-8', errors='replace')[:200]}"
                    )
                    return file_path, None
                shutil.move(str(produced[0]), cached_modern)
        except (subprocess.TimeoutExpired, OSError) as e:
            _pipeline_logger.warning(
                f"{src_suffix} → .{target_ext} conversion error for "
                f"{file_path.name}: {e}"
            )
            return file_path, None

    return cached_modern, {
        "step": "format_convert",
        "from": src_suffix.lstrip("."),
        "to": target_ext,
        "tool": "libreoffice",
    }


def _generate_page_images_via_libreoffice(
    file_path: Path,
    parse_result,
    config: dict[str, Any],
    max_pages: int | None,
    max_pixels: int,
    format_label: str,
) -> None:
    """
    Generic LibreOffice → PDF → page screenshots for formats without native page images.

    Works for any LibreOffice-supported format (xlsx, xls, docx, doc, pptx, odt, etc.).
    This is the shared backend for _ensure_excel_page_images and _ensure_docx_page_images.

    Args:
        file_path: Source file.
        parse_result: ParseResult to mutate (adds pages).
        config: Full config dict.
        max_pages: Cap on number of page images sent to Vision.
        max_pixels: Auto-downscale images exceeding this pixel count.
        format_label: "Excel" / "Word" / etc., used only in log messages and warnings.
    """
    import tempfile
    from .parsers.base import PageData
    from .utils.binary_finder import find_binary, run_soffice_convert

    # Already has images → nothing to do
    if parse_result.pages and any(p.image_path for p in parse_result.pages):
        return

    # Resolve LibreOffice via the cross-platform binary finder rather than
    # raw shutil.which — on Windows LibreOffice installs under Program Files
    # without touching PATH, and users can override via config or env var.
    soffice = find_binary("soffice", config)
    if not soffice:
        _pipeline_logger.debug(
            f"LibreOffice not found — cannot generate {format_label} page images"
        )
        return

    assets_dir = Path(get_nested(
        config, "output.dir", "./knowledge"
    )) / get_nested(config, "output.assets_dir", "assets")
    assets_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Step 1: → PDF via LibreOffice.
            #
            # NOTE: IsSkipEmptyPages was tried for xlsx but verified ineffective
            # — LibreOffice's "Suppress empty pages" is Writer-only (officially
            # documented as such, confirmed by isolated soffice test: 159 →
            # 158 page on a real workbook). xlsx blank pages come from
            # LibreOffice's "fit to page" rendering and have to be tolerated;
            # Docling openpyxl extraction provides cell-level fallback so
            # blank-page Vision noise doesn't lose information.
            # helper supplies the writable UserInstallation profile soffice
            # needs (esp. inside a packaged exe); None → not found, pdf_files
            # stays empty and we degrade via the existing check below.
            run_soffice_convert(
                file_path, tmpdir, "pdf", config=config, timeout=180,
            )
            pdf_files = list(Path(tmpdir).glob("*.pdf"))
            if not pdf_files:
                _pipeline_logger.debug(
                    f"LibreOffice produced no PDF for {file_path.name}"
                )
                return

            # Step 1.5: xlsx-only — read PDF outline to build sheet→page map.
            # Cheap (one pymupdf.open + get_toc, no rendering); the map is
            # consumed downstream by the connector hook and Vision batched
            # mode. Failure of any kind returns {} and downstream falls
            # back to the legacy 1-sheet-1-page assumption — this NEVER
            # decides whether a page is sent to Vision.
            if (format_label == "Excel"
                    and get_nested(config, "parsing.xlsx.sheet_page_map.enabled", True)):
                visible = parse_result.metadata.get("xlsx_visible_sheet_names")
                if isinstance(visible, list) and visible:
                    from .utils.xlsx_sheet_map import build_sheet_page_map
                    sheet_map = build_sheet_page_map(pdf_files[0], visible)
                    if sheet_map:
                        parse_result.metadata["xlsx_sheet_page_map"] = sheet_map

            # Step 1.5b: Word-only — extract each PDF page's text layer.
            # LibreOffice already decided the pagination, so the text layer
            # IS the page→content map docx otherwise lacks (its body text is
            # flow, not paged — §9.4). Consumed downstream as the per-page
            # Vision ground truth (sliced "already captured" reference) and
            # for anchoring embedded figures to pages. Same pattern as the
            # xlsx sheet map above: cheap (one pymupdf open, no rendering),
            # any failure leaves the metadata absent and downstream falls
            # back to the full-markdown ground truth (pre-slicing behaviour).
            if format_label == "Word":
                try:
                    import fitz  # type: ignore[import-not-found]
                    with fitz.open(str(pdf_files[0])) as _pdf:
                        page_texts = {
                            i + 1: _pdf[i].get_text() for i in range(len(_pdf))
                        }
                    if page_texts:
                        parse_result.metadata["docx_page_texts"] = page_texts
                except Exception as e:
                    _pipeline_logger.debug(
                        f"docx page-text extraction skipped ({e}) — per-page "
                        f"ground truth falls back to the full markdown"
                    )

            # Step 2: PDF → page images
            # Resolve target DPI from config (unified across all render paths)
            image_dpi = get_nested(config, "parsing.vision.image_dpi", 180)

            pages_data: list = []
            total_pages = 0

            try:
                from pdf2image import convert_from_path
                images = convert_from_path(str(pdf_files[0]), dpi=image_dpi)
                total_pages = len(images)
                for i, img in enumerate(images):
                    # max_pages may be None (no per-format cap; defers entirely
                    # to the global parsing.vision.max_pages enforcement
                    # downstream). int comparison would TypeError on None.
                    if max_pages is not None and i >= max_pages:
                        break
                    name = f"{file_path.stem}-page-{i + 1:03d}.png"
                    out = assets_dir / name
                    img.save(str(out))
                    _downscale_image(str(out), max_pixels)
                    pages_data.append(PageData(
                        page_no=i + 1,
                        text="",
                        image_path=str(out),
                    ))
            except Exception as e:
                # pdf2image unavailable (ImportError) OR pdf2image present but
                # its runtime deps missing — most commonly `PDFInfoNotInstalledError`
                # when the user has `pdf2image` (a pure-Python wrapper) but no
                # `poppler` system binary. Earlier this branch caught only
                # ImportError, so a poppler-less machine silently produced zero
                # page images and Vision was never invoked. Widening to Exception
                # routes ALL pdf2image failure modes to the Docling fallback
                # below — Docling renders PDF pages itself (no poppler needed),
                # so as long as `docling` is installed (it's a core dep) Vision
                # still gets its page images on machines without poppler.
                _pipeline_logger.warning(
                    f"pdf2image failed ({type(e).__name__}: {e}) — "
                    f"falling back to Docling PDF re-parse for page images. "
                    f"To use the faster pdf2image path, install poppler "
                    f"(Windows: `winget install oschwartz10612.Poppler`, "
                    f"macOS: `brew install poppler`, Linux: `apt install poppler-utils`)."
                )
                try:
                    from docling.document_converter import DocumentConverter, PdfFormatOption
                    from docling.datamodel.pipeline_options import PdfPipelineOptions
                    from docling.datamodel.base_models import InputFormat

                    opts = PdfPipelineOptions()
                    opts.generate_page_images = True
                    opts.images_scale = image_dpi / 72.0
                    conv = DocumentConverter(
                        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
                    )
                    pdf_result = conv.convert(str(pdf_files[0]))
                    total_pages = len(pdf_result.document.pages) if hasattr(pdf_result.document, "pages") else 0
                    for page_no, page in pdf_result.document.pages.items():
                        # See note above re: max_pages=None.
                        if max_pages is not None and len(pages_data) >= max_pages:
                            break
                        pil = getattr(page.image, "pil_image", None) if page.image else None
                        if pil is None:
                            continue
                        name = f"{file_path.stem}-page-{page_no:03d}.png"
                        out = assets_dir / name
                        pil.save(str(out))
                        _downscale_image(str(out), max_pixels)
                        pages_data.append(PageData(
                            page_no=page_no,
                            text="",
                            image_path=str(out),
                        ))
                except Exception as e2:
                    _pipeline_logger.warning(
                        f"Docling PDF re-parse fallback also failed "
                        f"({type(e2).__name__}: {e2}) — Vision will receive "
                        f"no page images for {file_path.name}, so Vision "
                        f"enrichment is effectively disabled for this file."
                    )

            # Apply results
            if pages_data:
                parse_result.pages = pages_data
                _pipeline_logger.info(
                    f"{format_label} page images: generated {len(pages_data)} pages "
                    f"via LibreOffice for {file_path.name}"
                )

            # Emit warning if pages were capped (visible in frontmatter + logs).
            # When max_pages is None there is no per-format cap; the global
            # parsing.vision.max_pages enforcement downstream handles the
            # cost ceiling and emits its own log.
            if max_pages is not None and total_pages > max_pages:
                warn_msg = (
                    f"{format_label} produced {total_pages} pages, "
                    f"only first {max_pages} sent to Vision "
                    f"(max_page_images={max_pages}). "
                    f"Remaining pages use text-only extraction."
                )
                _pipeline_logger.warning(warn_msg)
                warnings = parse_result.metadata.setdefault("warnings", [])
                warnings.append(warn_msg)

    except Exception as e:
        # Surface as WARNING (was DEBUG) — page-image generation failure
        # silently disables Vision for the file, which is a quality regression
        # users need to see in the default log, not just when -v is on.
        _pipeline_logger.warning(
            f"{format_label} page image generation failed "
            f"({type(e).__name__}: {e}) — Vision enrichment will be skipped "
            f"for {file_path.name}."
        )


def _office_file_has_no_visuals(
    parse_result,
    config: dict[str, Any],
    fmt: str,
) -> bool:
    """True when the WHOLE file is provably text-only → page Vision (and the
    LibreOffice render that exists only to feed it) can be skipped.

    The parser collected the evidence from the raw OOXML:
      - docx: ``docx_visual_signals`` — drawing / VML / ink / highlight /
        tracked-change counts in the body (oracle-tested: on signal-free
        files Vision's only output was header/footer furniture; ink and
        highlight correctly flagged annotated review docs as visual).
      - xlsx: ``xlsx_sheet_visuals`` — per-sheet drawing/ole element counts;
        ALL visible sheets must be zero (any sheet missing from the scan
        counts as visual).

    Conservative by construction: absent metadata (scan failed, legacy
    format, Docling-fallback xlsx path) or a disabled config switch → False
    → the full current behaviour runs. Never decides to skip on missing
    evidence."""
    meta = parse_result.metadata or {}
    if fmt == "docx":
        if not get_nested(config, "parsing.docx.vision.skip_text_only", True):
            return False
        signals = meta.get("docx_visual_signals")
        return isinstance(signals, dict) and bool(signals) \
            and not any(signals.values())
    if fmt == "xlsx":
        if not get_nested(config, "parsing.xlsx.vision.sheet_triage", True):
            return False
        visuals = meta.get("xlsx_sheet_visuals")
        visible = meta.get("xlsx_visible_sheet_names")
        if not isinstance(visuals, dict) or not visuals \
                or not isinstance(visible, list) or not visible:
            return False
        return all(visuals.get(name, 1) == 0 for name in visible)
    return False


def _ensure_excel_page_images(
    file_path: Path,
    parse_result,
    config: dict[str, Any],
) -> None:
    """
    Excel-specific page image fallback (thin wrapper around the generic backend).

    Reads config from parsing.xlsx.denoising.{ensure_page_images, max_page_images, max_image_pixels}.
    """
    xlsx_denoise = get_nested(config, "parsing.xlsx.denoising", {})
    if not xlsx_denoise.get("ensure_page_images", True):
        return

    _generate_page_images_via_libreoffice(
        file_path=file_path,
        parse_result=parse_result,
        config=config,
        max_pages=xlsx_denoise.get("max_page_images", 10),
        max_pixels=xlsx_denoise.get("max_image_pixels", 4_000_000),
        format_label="Excel",
    )


def _ensure_docx_page_images(
    file_path: Path,
    parse_result,
    config: dict[str, Any],
) -> None:
    """
    Word-specific page image fallback (thin wrapper around the generic backend).

    Docling's DOCX pipeline extracts text but does NOT produce page images,
    so embedded diagrams and figures are lost. This function renders the DOCX
    via LibreOffice → PDF → screenshots, feeding them to Vision for description.

    Reads config from parsing.docx.{vision_page_images, max_page_images, max_image_pixels}.
    """
    docx_cfg = get_nested(config, "parsing.docx", {})
    if not docx_cfg.get("vision_page_images", True):
        return

    _generate_page_images_via_libreoffice(
        file_path=file_path,
        parse_result=parse_result,
        config=config,
        max_pages=docx_cfg.get("max_page_images", 20),
        max_pixels=docx_cfg.get("max_image_pixels", 4_000_000),
        format_label="Word",
    )


def _ensure_pptx_page_images(
    file_path: Path,
    parse_result,
    config: dict[str, Any],
) -> None:
    """
    PPT-specific page image fallback (thin wrapper around the generic backend).

    DoclingParser has an older PPT fallback in _try_external_page_images that
    only triggers when Docling returned pages_data but with empty image_paths.
    If Docling returns no pages at all (some PPT backends / simple pipeline),
    this Phase 1.3 hook fills the gap — it creates pages from scratch via
    LibreOffice rendering so Vision can still describe every slide.

    The generic backend's first check `parse_result.pages and any(p.image_path)`
    means this is a no-op when the parser-level fallback already succeeded,
    so there's no double work.

    Reads config from parsing.pptx.{vision_page_images, max_page_images, max_image_pixels}.
    """
    pptx_cfg = get_nested(config, "parsing.pptx", {})
    if not pptx_cfg.get("vision_page_images", True):
        return

    _generate_page_images_via_libreoffice(
        file_path=file_path,
        parse_result=parse_result,
        config=config,
        max_pages=pptx_cfg.get("max_page_images", 30),
        max_pixels=pptx_cfg.get("max_image_pixels", 4_000_000),
        format_label="PPT",
    )


# ---------------------------------------------------------------------------
# Vision enrichment
# ---------------------------------------------------------------------------

def _should_skip_vision(
    page_data,
    structured_per_page: dict,
    triage_cfg: dict[str, Any],
    doc_language: str | None = None,
) -> bool:
    """
    Conservative triage: decide if a page can safely skip Vision enrichment.

    ALL conditions must be met to skip. Any single failure → send to Vision.
    This ensures we never miss important content — it's acceptable to send
    a few extra pages (false negatives are cheap, false positives lose info).

    Checks (all must pass to skip):
      1. No image markers in Docling output (no charts/diagrams to describe)
      2. No picture elements (Docling figures / fitz-detected embedded images)
      3. No structured data injection (no chart hook data needing visual context)
      4. Sufficient text extracted (not a scanned/image-only page)
      5. No garbled text (glyph< CID failure, or CJK-adjacent &lt; entity garble)
      6. Low replacement character ratio (no CID font issues)
      7. No complex tables (simple tables are fine, complex ones need Vision)
      8. No mixed-script anomaly (CJK mismap garbling from OCR)
      9. Text scripts match the document's declared language (catches CMap
         failures that produce "clean" but wrong Unicode — e.g. Bengali /
         Thai / Tibetan characters in a document declared as Japanese).
     10. No Latin-script cipher garble (catches a broken CMap that maps each
         glyph to a *different* legal Latin letter — the output is clean ASCII,
         no glyph< / U+FFFD / CJK / unexpected script, so checks 5-9 all pass,
         but the text is an unreadable substitution cipher. Flagged by an
         abnormally low vowel ratio. Latin-script docs only.)
    """
    text = page_data.text
    stripped = text.strip()

    # Furniture exemption (opt-in, PDF only, default OFF). A page whose ONLY
    # pictures are cross-page furniture (repeating small logos / headers /
    # footers, detected in docling_parser._detect_furniture_pictures) carries
    # no per-page visual information — sending it to Vision just to describe a
    # logo is wasted cost. We exempt such a page from the two "has a picture →
    # Vision" gates below, BUT ONLY when it is otherwise a clean text page:
    #   * EVERY picture on the page is furniture (num_pictures == furniture
    #     count) — a single non-furniture picture (a chart, a stamp, even a
    #     small one) keeps the page on the Vision path.
    #   * NO table markup at all — not even one `|` row. We do NOT use the
    #     table_line_threshold here: a small table (below threshold) would
    #     normally rely on Docling's own text, but once we stop sending the
    #     page to Vision we must be stricter. Any table hint → keep on Vision.
    #   * The remaining triage layers (text length, garble, replacement ratio,
    #     script consistency) still run below and can independently veto the
    #     skip. Furniture exemption only neutralises the picture signal; it
    #     never forces a skip on its own.
    # Single-page images are never furniture (the detector requires cross-page
    # repetition), so a page that disguises body text as an image is safe —
    # that image is single-page → not furniture → page still goes to Vision.
    num_pics = getattr(page_data, "num_pictures", 0)
    furn_pics = getattr(page_data, "furniture_pic_count", 0)
    has_any_table_markup = any(
        "|" in line and line.strip().startswith("|")
        for line in stripped.split("\n")
    )
    furniture_only_page = (
        num_pics > 0
        and furn_pics >= num_pics
        and not has_any_table_markup
        and "<!-- image -->" not in text  # an explicit non-furniture image marker
    )

    # Has image/chart markers → Vision needs to describe visual elements
    if "<!-- image -->" in text:
        return False

    # Page carries picture elements (Docling figures or, for PDF, embedded
    # images detected by fitz) → Vision must describe them. This is the
    # OCR-INDEPENDENT image signal: without it, "invisible image pages" — a
    # figure Docling did not render as <!-- image --> and whose ONLY previous
    # trigger was OCR garble — get wrongly skipped once Docling OCR is off.
    # Furniture-only pages are the deliberate exception (see above).
    if getattr(page_data, "num_pictures", 0) > 0 and not furniture_only_page:
        return False

    # Has structured data (e.g. PPTX chart) → Vision describes surrounding visuals
    if structured_per_page.get(page_data.page_no):
        return False

    # Too little text → possibly scanned/image-only page
    min_len = int(triage_cfg.get("min_text_length", 50))
    if len(stripped) < min_len:
        return False

    # Garbled text → Docling failed, Vision can re-OCR
    # glyph< = CID font mapping failure
    if "glyph<" in text or "glyph&lt;" in text:
        return False
    # CJK-adjacent &lt; entity = OCR garble where a CJK char was truncated into
    # an HTML entity (e.g. 確認 → 確&lt;). We match ONLY a CJK char immediately
    # followed by &lt; — NOT a bare &lt;/&gt; anywhere — because legitimate
    # Japanese headings wrap titles in <…> which Docling escapes to
    # `## &lt;入居中の各種連絡先&gt;` (entity BEFORE the CJK, plus a closing
    # &gt;), and English/code prose uses &lt; legitimately (`vector&lt;int&gt;`).
    # Measured on real corpus: 8/8 garble pages caught, 2/2 heading pages
    # skipped, code/HTML prose not flagged. Toggle via
    # parsing.vision.triage.entity_garble_check.
    if triage_cfg.get("entity_garble_check", True) and _CJK_ENTITY_GARBLE_RE.search(stripped):
        return False

    # High U+FFFD ratio → CID font extraction failure
    max_fffd = float(triage_cfg.get("max_replacement_ratio", 0.05))
    if len(stripped) > 0 and stripped.count("\ufffd") / len(stripped) > max_fffd:
        return False

    # Complex Markdown table → Vision may help correct structure
    table_threshold = int(triage_cfg.get("table_line_threshold", 10))
    table_lines = sum(
        1 for line in stripped.split("\n")
        if "|" in line and line.strip().startswith("|")
    )
    if table_lines >= table_threshold:
        return False

    # Mixed-script anomaly → CJK mismap garbling (e.g. する→寸, 査→查)
    # Detects short ASCII fragments (1-3 chars) sandwiched between CJK chars,
    # which is characteristic of OCR engines misreading embedded text images.
    # Normal text (e.g. "PwC" in Japanese docs) has whitespace/punctuation
    # around ASCII, not direct CJK adjacency.
    if _has_mixed_script_anomaly(stripped, triage_cfg):
        return False

    # Language-script consistency — catches PDFs whose font CMap produced
    # clean but *wrong* Unicode (Bengali / Thai / Tibetan chars on a page
    # declared as Japanese). The other garble checks miss this case because
    # the output is legal Unicode, no glyph< markers, no U+FFFD, no CJK.
    if _has_unexpected_scripts(stripped, triage_cfg, doc_language):
        return False

    # Latin-script substitution-cipher garble — a broken font CMap that maps
    # each glyph to a DIFFERENT legal Latin letter. The output is clean ASCII
    # (no glyph<, no U+FFFD, no CJK, scripts all "Latin"), so every check above
    # passes, yet the text is unreadable. The signature is an abnormally low
    # vowel ratio: real Latin prose runs ~30-45% vowels, a permutation collapses
    # it to single digits. Latin-script pages only (CJK-dominant pages are
    # excluded inside the check). See language_script_check for the sibling case
    # where the garble lands in a *different* script entirely.
    if _has_latin_cipher_garble(stripped, triage_cfg):
        return False

    # All checks passed → pure text page, safe to skip
    return True


# Shared CJK character class for the two OCR-garble triage checks below. Covers
# Han + Hiragana + Katakana + Hangul, matching the languages the language-script
# check (parsing.vision.triage.language_script_check) already supports (ja/zh/
# ko). Defined once so both checks stay in sync \u2014 adding a script here updates
# both. (English is Latin and never produces CJK-truncation garble, so it needs
# no entry.)
_CJK_CLASS = r"\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af"

# Pre-compiled: a CJK char immediately followed by the &lt; HTML entity \u2014 the
# signature of OCR garble that truncated a CJK char into an entity (\u78ba\u8a8d \u2192 \u78ba&lt;).
# Deliberately NOT matching a closing &gt; or a leading entity: CJK headings
# legitimately read `## &lt;\u2026&gt;` (entity precedes the CJK, closing &gt; follows a
# CJK char), so matching those directions would misfire on valid titles.
_CJK_ENTITY_GARBLE_RE = re.compile(rf"[{_CJK_CLASS}]&lt;")

# Pre-compiled pattern for mixed-script detection (module-level, compiled once):
# a short ASCII fragment sandwiched between two CJK chars (OCR mismap garble).
_MIXED_SCRIPT_RE = re.compile(rf"[{_CJK_CLASS}][A-Za-z]{{1,3}}[{_CJK_CLASS}]")


def _has_mixed_script_anomaly(text: str, triage_cfg: dict[str, Any]) -> bool:
    """
    Detect CJK mismap garbling by counting mixed-script fragments.

    When Docling's OCR engine misreads embedded text images in CJK documents,
    it produces characteristic patterns: short ASCII fragments directly adjacent
    to CJK characters (e.g. 問PW問, 儿PwC速, 活用I二寸).

    Uses absolute fragment count rather than ratio — even one page with garbled
    OCR typically produces 3+ fragments, while normal CJK text with inline
    brand names (PwC, JICPA) uses spacing/punctuation around ASCII, producing
    0-2 matches at most.
    """
    if not text:
        return False
    max_fragments = int(triage_cfg.get("max_mixed_script_fragments", 3))
    fragment_count = len(_MIXED_SCRIPT_RE.findall(text))
    return fragment_count > max_fragments


def _has_unexpected_scripts(
    text: str,
    triage_cfg: dict[str, Any],
    doc_language: str | None,
) -> bool:
    """
    Detect text whose Unicode scripts don't match the document's declared
    language — the only signal strong enough to catch CMap failures that
    produce valid but semantically-wrong characters.

    Behaviour:
      * feature disabled in config → False
      * text shorter than min_text_length_for_check → False (too noisy)
      * doc_language known and in expected_scripts → enforce that whitelist
      * doc_language unknown or not configured → enforce a generic
        "common scripts only" whitelist (Latin, Han, Hiragana, Katakana,
        Hangul). This catches garbled CMap output even when language
        detection itself failed (e.g. Bengali/Thai chars on a page whose
        declared language is "unknown" because detection was thrown off
        by the garble).

    The generic-whitelist branch is intentionally permissive — it accepts
    every language we have explicit support for today, so it cannot
    mis-flag a legitimate Japanese / Chinese / Korean / English document
    that failed language detection. It only flags content that is in
    NONE of those major scripts.
    """
    lsc_cfg = triage_cfg.get("language_script_check")
    if not isinstance(lsc_cfg, dict) or not lsc_cfg.get("enabled", False):
        return False

    min_len = int(lsc_cfg.get("min_text_length_for_check", 50))
    if len(text) < min_len:
        return False

    expected_map = lsc_cfg.get("expected_scripts") or {}
    if not isinstance(expected_map, dict):
        return False

    expected_list = expected_map.get(doc_language) if doc_language else None
    if not expected_list:
        # Unknown / unconfigured language — fall back to the union of all
        # configured language whitelists. If the detected text uses only
        # scripts that ANY supported language would accept, we don't flag;
        # exotic scripts (Bengali / Thai / Tibetan from CMap failure)
        # still trigger even when language detection failed upstream.
        union_scripts: set[str] = set()
        for scripts in expected_map.values():
            if isinstance(scripts, list):
                union_scripts.update(scripts)
        if not union_scripts:
            return False
        expected_list = list(union_scripts)

    max_ratio = float(lsc_cfg.get("max_unexpected_script_ratio", 0.05))

    # Import here to avoid circular-ish module load at startup; the import
    # is cheap (stdlib only) and only fires when the check runs.
    from .utils.script_detector import unexpected_script_ratio

    ratio, _ = unexpected_script_ratio(text, set(expected_list))
    return ratio > max_ratio


# Unicode ranges for the four CJK scripts the triage layers already reason about
# (Han + Hiragana + Katakana + Hangul). Used by the Latin-cipher check to gate
# itself OFF on CJK-dominant pages — those have their own garble detectors
# (mixed-script anomaly, language-script consistency) and a handful of inline
# Latin words must never make a Japanese/Chinese/Korean page look like Latin
# cipher. Kept as plain tuples (not regex) so the single-pass char scan stays
# allocation-free.
_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK unified ideographs (Han)
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul syllables
)


def _has_latin_cipher_garble(text: str, triage_cfg: dict[str, Any]) -> bool:
    """Detect a Latin-script substitution-cipher garble via an abnormally low
    vowel ratio.

    Catches the one CMap-failure mode the other triage layers miss: a broken
    font CMap that maps each glyph to a *different* but still legal Latin letter
    (e.g. "Nutrition" → "Pgvsdvdfp"). The output is clean ASCII — no glyph<
    markers, no U+FFFD, no CJK, every character classifies as the "Latin"
    script — so checks 5-9 all pass and the page would be skipped. The only
    surviving signal is statistical: natural Latin-script prose carries ~30-45%
    vowels, while a letter permutation almost always maps the original a/e/i/o/u
    onto consonants, collapsing the apparent vowel ratio into the single digits.

    Validated on real corpus (164 genuine EN/JA page blocks → 0 false positives;
    222 ciphered pages across two cipher families → 100% caught). The default
    0.20 threshold sits in a wide gap: real EN pages bottomed out at 29% vowels,
    ciphered pages topped out at 19%.

    Gated three ways to stay conservative (any gate not met → returns False,
    i.e. "not garble", so the page proceeds through normal triage):
      * disabled in config → off entirely.
      * too few Latin letters to judge → off (short labels, number-heavy pages,
        non-Latin scripts all fall here — they can't be cipher-flagged alone).
      * CJK-dominant page → off (those have their own garble detectors; a few
        inline English words must not trip this).

    All thresholds are config-driven (parsing.vision.triage.latin_cipher_check).
    """
    cfg = triage_cfg.get("latin_cipher_check")
    # Default ON when the sub-config is absent: this is a safety net (a triggered
    # check only sends a page TO Vision — it can never drop content), so a config
    # that predates this layer should still get the protection.
    if isinstance(cfg, dict) and not cfg.get("enabled", True):
        return False
    if not isinstance(cfg, dict):
        cfg = {}

    min_letters = int(cfg.get("min_letters", 30))
    max_vowel_ratio = float(cfg.get("max_vowel_ratio", 0.20))
    max_cjk_share = float(cfg.get("max_cjk_share", 0.40))

    letters = 0
    vowels = 0
    cjk = 0
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            letters += 1
            if ch in "aeiouAEIOU":
                vowels += 1
        else:
            cp = ord(ch)
            for lo, hi in _CJK_RANGES:
                if lo <= cp <= hi:
                    cjk += 1
                    break

    # Too little Latin text to make a statistical call.
    if letters < min_letters:
        return False
    # CJK-dominant page — out of scope for the Latin-vowel heuristic.
    if cjk / (cjk + letters) > max_cjk_share:
        return False

    return vowels < max_vowel_ratio * letters


def _is_vision_critical_page(
    page_text: str,
    triage_cfg: dict[str, Any],
    sysf_cfg: dict[str, Any],
) -> bool:
    """Is this a page whose CONTENT depends on Vision succeeding?

    Used only by the systemic-failure check at the end of _enrich_with_vision:
    when EVERY Vision page failed, we look at the failed pages' Docling text to
    decide whether the failure actually cost us content (fail loud) or merely
    cost a visual supplement on top of usable Docling text (stay silent).

    A failed page is "critical" iff its Docling text is either:
      (a) Empty of meaning — once the structural placeholders Docling emits for
          image-only pages ("<!-- image -->", "<!-- pagebreak -->") and all
          whitespace are stripped, fewer than `min_meaningful_chars` real
          characters remain. This is the scanned-document case: Docling
          extracted nothing, Vision was the ONLY content source, and it failed.
          (NB: a true scanned page's text is "<!-- image -->", NOT "" — judging
          by len()==0 alone would miss it, which is why we strip placeholders.)
      (b) Garbled — the Docling text tripped one of the existing garble signals
          (CID-mapping "glyph<" failure, a high U+FFFD ratio, a mixed-script
          CJK mismap, or a Latin substitution-cipher CMap break). Vision was
          enriching specifically to REPAIR this broken text; losing it means the
          page is left as unreadable garble.

    A page whose Docling text is clean and non-trivial is NOT critical: Vision
    there was only adding a visual supplement, so even a total Vision failure
    leaves the page's real content intact (handled as ordinary fallback).

    Thresholds: min_meaningful_chars from sysf_cfg (parsing.vision.systemic_failure);
    the garble ratio/helpers from triage_cfg — the SAME ones triage already uses,
    so "what counts as garble" stays defined in exactly one place.
    """
    text = page_text or ""

    # (a) Meaningless-after-placeholder-strip → empty content page (scanned).
    min_meaningful = int(sysf_cfg.get("min_meaningful_chars", 10))
    stripped = (
        text.replace("<!-- image -->", "")
        .replace("<!-- pagebreak -->", "")
        .strip()
    )
    if len(stripped) < min_meaningful:
        return True

    # (b) Garble — reuse the exact signals triage uses, so the definition of
    # "broken text Vision was meant to repair" lives in one place.
    if "glyph<" in text or "glyph&lt;" in text:
        return True
    max_fffd = float(triage_cfg.get("max_replacement_ratio", 0.05))
    if len(stripped) > 0 and stripped.count("�") / len(stripped) > max_fffd:
        return True
    if _has_mixed_script_anomaly(stripped, triage_cfg):
        return True
    if _has_latin_cipher_garble(stripped, triage_cfg):
        return True

    return False


def _is_empty_supplement(text: str) -> bool:
    """In supplement mode, Vision returns "(no additional visual content)" when
    Docling already captured the page. Detect that sentinel (tolerant of minor
    punctuation/whitespace) so we emit no vision-enriched block for the page."""
    t = text.strip().strip(".。").lower()
    return t in ("(no additional visual content)", "no additional visual content")


def _xlsx_batch_ground_truth_slice(
    sections: list[str],
    visible: list[str],
    page_map: dict[str, int],
    batch_page_nos: list[int],
    total_pages: int,
) -> str | None:
    """Ground truth for ONE batch: only the sheet sections its pages cover.

    A batched supplement call needs "what's already captured" for ITS pages —
    feeding the whole workbook repeats the full markdown once per batch
    (measured: 5 × 192k chars on a 159-page workbook, the dominant input-token
    share). Attribution uses the same anchored-range rule as the sheet triage:
    a mapped sheet owns pages from its first page up to the next MAPPED
    sheet's first page − 1 (the last mapped sheet owns through
    ``total_pages``) — but only when no UNMAPPED visible sheet follows it in
    workbook order (its pages, if any, would fall inside that range and the
    attribution would lie).

    Returns None when ANY batch page can't be confidently attributed — the
    caller then sends the full markdown. A wrong slice is the one outcome to
    avoid: the supplement prompt treats ground truth that doesn't match the
    images as "extraction failed" and re-transcribes in full (cost and
    duplication come back; nothing is lost, but the saving inverts).

    Caller guarantees ``len(sections) == len(visible)`` (sections split on
    PAGEBREAK_MARKER from the openpyxl render, one per visible sheet).
    """
    ordered_mapped = [s for s in visible if s in page_map]
    if not ordered_mapped:
        return None
    anchors = [page_map[s] for s in ordered_mapped]
    if anchors != sorted(set(anchors)):
        return None

    covered: set[int] = set()
    for p in batch_page_nos:
        owner_idx: int | None = None
        for i, s in enumerate(ordered_mapped):
            start = page_map[s]
            end = (
                page_map[ordered_mapped[i + 1]] - 1
                if i + 1 < len(ordered_mapped) else total_pages
            )
            if start <= p <= end:
                vi = visible.index(s)
                nxt = visible[vi + 1] if vi + 1 < len(visible) else None
                if nxt is not None and nxt not in page_map:
                    return None
                owner_idx = vi
                break
        if owner_idx is None:
            return None
        covered.add(owner_idx)
    return f"\n{PAGEBREAK_MARKER}\n".join(sections[i] for i in sorted(covered))


def _xlsx_per_page_ground_truth(
    markdown: str,
    visible: list[str] | None,
    page_map: dict[str, int] | None,
    page_nos: list[int],
    total_pages: int,
) -> dict[int, str]:
    """Per-page ground-truth slices for the per-page Vision path.

    The per-page supplement path feeds ``parse_result.markdown`` (the whole
    workbook) to EVERY page call — N pages repeat the full markdown N times
    (measured: 16 calls × ~19k tokens on a 28-sheet workbook where each
    page's own sheet is ~0.7k). This mirrors the batched path's slicing with
    identical attribution + fallback semantics: each page is attributed via
    ``_xlsx_batch_ground_truth_slice`` as a single-page batch; any doubt for
    a page leaves it OUT of the dict (the caller falls back to the full
    markdown for that page). Returns ``{}`` when slicing can't arm at all
    (missing metadata, section/sheet count mismatch — e.g. docx has no sheet
    map, a hook reshaped the markdown) — caller behaviour is then byte-
    identical to before this function existed."""
    if not (
        isinstance(visible, list) and visible
        and isinstance(page_map, dict) and page_map
    ):
        return {}
    sections = markdown.split(PAGEBREAK_MARKER)
    if len(sections) != len(visible):
        return {}
    out: dict[int, str] = {}
    for p in page_nos:
        sliced = _xlsx_batch_ground_truth_slice(
            sections, visible, page_map, [p], total_pages
        )
        if sliced is not None:
            out[p] = sliced
    return out


def _demote_headings(text: str) -> str:
    """Markdown headings in a Vision SUPPLEMENT/figure block → bold lines.

    Vision transcriptions emit `#`/`###` for chart titles and axis legends.
    For full-mode pages (PDF/PPT) those headings ARE the page's structure —
    they must stay. For supplement blocks and embedded-figure transcriptions
    they are *figure-internal* labels, yet they enter the document's heading
    hierarchy: on a thesis whose own chapters were bold paragraphs (no Word
    heading styles), figure titles became the ONLY headings and 118/325
    chunks carried a chart's name as their title_path. Demoting to bold
    keeps every word (chunk text, grep, RAG recall unchanged) and removes
    only the structural claim. Fenced code blocks are left untouched."""
    out: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        m = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        out.append(f"**{m.group(2)}**" if m else line)
    return "\n".join(out)


def _gt_norm(s: str) -> str:
    """NFKC + whitespace-stripped normalisation for ground-truth text matching
    (PDF text layer line-wraps and markdown decoration must not break it)."""
    import unicodedata
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


def _docx_per_page_ground_truth(
    markdown: str,
    page_texts: dict[int, str] | None,
    page_nos: list[int],
) -> dict[int, str]:
    """Per-page ground-truth slices for docx: PDF page text + that page's
    already-transcribed embedded figures.

    docx has no sheet→page map, but the LibreOffice render that produced the
    page images also produced a PDF whose text layer IS the page→content map
    (extracted in Step 1.5b as ``docx_page_texts``). Two parts per page:

    - The page's own PDF text — what the body extraction already captured,
      so the supplement prompt knows not to re-transcribe body text.
      Measured: ~0.8k tokens/page vs ~102k for the full markdown (A/B
      verified: a page whose table was already captured answered "(no
      additional visual content)" instead of re-transcribing it).
    - The page's embedded-figure transcriptions — without them Vision
      re-transcribes figures it can SEE on the page image but that were
      already read at full resolution (A/B verified both ways: present →
      correction-style supplement preserved; absent → re-transcription).
      Figures are attributed to pages by anchoring: the nearest body line
      above each ``<!-- image: f -->`` marker, located in exactly ONE
      page's text (measured 63/71 unique on a 92-page thesis). A figure
      whose anchor is ambiguous or missing joins NO page — worst case it
      is re-transcribed by its page's call (content never lost), same
      fail-open posture as the xlsx slicer.

    Returns {} when page texts are absent (LibreOffice/pymupdf failed, or
    not a docx) — caller falls back to the full markdown, the pre-slicing
    behaviour. An empty string for a page is meaningful, not a failure:
    a scanned page has no text layer, and an empty ground truth correctly
    triggers the supplement prompt's full-transcription escape."""
    if not (isinstance(page_texts, dict) and page_texts):
        return {}
    norm_pages = {p: _gt_norm(t) for p, t in page_texts.items()}

    lines = markdown.split("\n")

    def _anchor_page(i: int, direction: int) -> int | None:
        """Nearest body line above (direction=-1) / below (+1) the marker
        that is long enough to anchor, located in exactly ONE page's text.
        Below catches the figure-caption convention (図N.N right under the
        image) when the text above is another marker or a short heading."""
        rng = (
            range(i - 1, max(-1, i - 16), -1) if direction < 0
            else range(i + 1, min(len(lines), i + 16))
        )
        for j in rng:
            if lines[j].lstrip().startswith("<!--"):
                continue
            cand = _gt_norm(re.sub(r"[*_`#>|]", "", lines[j]))
            if len(cand) < 20:
                continue
            anchor = cand[-30:] if direction < 0 else cand[:30]
            hits = [p for p, pn in norm_pages.items() if anchor in pn]
            return hits[0] if len(hits) == 1 else None
        return None

    # 1) Anchor each image marker to a page (above first, caption below as
    #    fallback — measured 63/71 → 65/71 unique on a 92-page thesis).
    fig_page: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = re.search(r"<!-- image: (.+?) -->", line)
        if not m:
            continue
        page = _anchor_page(i, -1)
        if page is None:
            page = _anchor_page(i, +1)
        if page is not None:
            fig_page[m.group(1)] = page

    # 2) Collect each figure's transcription block (marker line up to the
    #    next comment marker).
    fig_block: dict[str, str] = {}
    cur_name: str | None = None
    cur_buf: list[str] = []
    for line in lines:
        m = re.search(r"<!-- vision-enriched image=(.+?) -->", line)
        if m:
            if cur_name is not None:
                fig_block[cur_name] = "\n".join(cur_buf).strip()
            cur_name, cur_buf = m.group(1), []
            continue
        if cur_name is not None:
            if line.lstrip().startswith("<!--"):
                fig_block[cur_name] = "\n".join(cur_buf).strip()
                cur_name, cur_buf = None, []
            else:
                cur_buf.append(line)
    if cur_name is not None:
        fig_block[cur_name] = "\n".join(cur_buf).strip()

    # 3) Assemble per-page ground truth.
    out: dict[int, str] = {}
    for p in page_nos:
        if p not in page_texts:
            continue
        gt = page_texts[p].strip()
        figs = [
            fig_block[f] for f, pg in fig_page.items()
            if pg == p and fig_block.get(f)
        ]
        if figs:
            gt += (
                "\n\n[Embedded figures on this page, already transcribed]:\n\n"
                + "\n\n".join(figs)
            )
        out[p] = gt
    return out


def _inject_after_marker(markdown: str, filename: str, block: str) -> str:
    """Insert ``block`` on its own lines after the `<!-- image: file -->` marker.

    Structural rule (not a format branch): when the marker's line is a table
    row (xlsx cell-anchored markers are wrapped as ``| <!-- image: f --> | |``
    so they don't split the table), the block is deferred to just after that
    table ends — injecting mid-table would cut it in two and re-trigger the
    SheetChunker header-duplication bug the wrapping exists to prevent. A
    marker on its own line (docx) gets the block immediately after it.

    If the marker isn't found (it was cleaned/merged away), the block is
    appended at the end tagged with the filename so the transcription is
    never lost. Returns the markdown unchanged only when ``block`` is empty."""
    if not block.strip():
        return markdown
    marker = f"<!-- image: {filename} -->"
    tagged = [f"<!-- vision-enriched image={filename} -->", *block.strip().split("\n")]
    if marker not in markdown:
        return markdown.rstrip() + "\n\n" + "\n".join(tagged) + "\n"

    lines = markdown.split("\n")
    idx = next(i for i, ln in enumerate(lines) if marker in ln)
    if lines[idx].lstrip().startswith("|"):
        insert_at = idx + 1
        while insert_at < len(lines) and lines[insert_at].lstrip().startswith("|"):
            insert_at += 1
    else:
        insert_at = idx + 1
    lines[insert_at:insert_at] = ["", *tagged, ""]
    return "\n".join(lines)


def _enrich_embedded_images(
    parse_result,
    config: dict[str, Any],
    cache,
    doc_format: str | None,
) -> None:
    """Send above-threshold embedded figures to Vision at full resolution and
    inject each transcription after its `<!-- image: file -->` marker.

    The parser already saved every embedded picture and flagged which clear
    the size threshold via ``send_vision`` (docx: _extract_docling_pictures;
    xlsx: the openpyxl renderer — both write the same ``embedded_images``
    metadata shape). This stage only runs the Vision read — kept out of the
    parser so all Vision calls share one place (cache, cost tally, config).

    Modifies ``parse_result.markdown`` in place. No-op unless the file produced
    ``embedded_images`` and ``image_extraction.vision_enrich`` is on. The
    per-file ``max_images_vision`` cap (per-format config), on trip, warns and
    skips the remainder — the assets and markers are already written, so
    nothing is lost; only the extra full-res read is deferred (never a silent
    truncation)."""
    import logging
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .parsers.vision import describe_embedded_image_cached

    logger = logging.getLogger(__name__)

    images = parse_result.metadata.get("embedded_images") or []
    to_send = [
        img for img in images
        if img.get("send_vision") and img.get("path") and img.get("filename")
    ]
    if not to_send:
        return

    cap_key = f"parsing.{(doc_format or '').lower()}.image_extraction.max_images_vision"
    # null (default) = read every qualifying figure; cost control lives in
    # the Phase-0 safety estimate, which prices the full count (same
    # anti-truncation stance as max_page_images / vision.max_pages).
    cap_raw = get_nested(config, cap_key, None)
    max_n = int(cap_raw) if cap_raw is not None else len(to_send)
    if len(to_send) > max_n:
        logger.warning(
            f"Embedded-image Vision: {len(to_send)} figure(s) qualify but cap is "
            f"{max_n} — reading the first {max_n}, skipping {len(to_send) - max_n}. "
            f"Assets + markers for the skipped ones are still written; raise "
            f"{cap_key} to read them too."
        )
        to_send = to_send[:max_n]

    # Parallel like the per-page pass (same worker knob) — figures are
    # independent calls, and serially they dominate wall time: measured 109 s
    # serial vs 37 s at parallel=4 for 6 figures on one review doc.
    parallel = get_nested(config, "performance.parallel_files", 4)

    def _read_figure(img: dict) -> tuple[dict, str | None, Exception | None]:
        # External stop: not-yet-started figure reads bail out instantly
        # (asset + marker are already written, only the transcription is
        # skipped — same degrade as a failed figure, minus the warning).
        if _EXTERNAL_STOP.is_set():
            return img, None, None
        try:
            with _vision_permit():
                return img, describe_embedded_image_cached(
                    image_path=img["path"],
                    config=config,
                    cache=cache,
                    doc_format=doc_format,
                ), None
        except Exception as e:
            return img, None, e

    failed = 0
    descs: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = [executor.submit(_read_figure, img) for img in to_send]
        for future in as_completed(futures):
            img, desc, err = future.result()
            if err is not None:
                # A single figure failing must not abort the file — the marker
                # and asset stay, the figure is simply not transcribed. Mirrors
                # the per-page Vision fallback.
                failed += 1
                logger.warning(
                    f"Embedded-image Vision failed for {img['filename']}: {err}"
                )
            elif desc and desc.strip():
                # Figure-internal chart titles must not enter the document's
                # heading hierarchy (see _demote_headings).
                if get_nested(
                    config, "parsing.vision.supplement_demote_headings", True
                ):
                    desc = _demote_headings(desc)
                descs[img["filename"]] = desc

    # Inject in REVERSE figure order: when several markers sit inside the same
    # table (xlsx), each block lands after that table's end — injecting in
    # forward order would stack later figures' blocks ABOVE earlier ones
    # (the table scan stops at the previously injected block). Reverse order
    # makes the final blocks read in figure order. Deterministic across runs
    # either way (never completion order).
    enriched = 0
    for img in reversed(to_send):
        desc = descs.get(img["filename"])
        if desc:
            parse_result.markdown = _inject_after_marker(
                parse_result.markdown, img["filename"], desc
            )
            enriched += 1

    # Record alongside the per-page Vision tally so run_log / quality_report can
    # surface the extra reads (cost visibility — these are additional API calls).
    parse_result.metadata.setdefault("vision_triage", {}).update({
        "embedded_sent": len(to_send),
        "embedded_enriched": enriched,
        "embedded_failed": failed,
    })
    logger.info(
        f"Embedded-image Vision: {enriched} figure(s) transcribed, "
        f"{failed} failed (of {len(to_send)} sent)."
    )


def _enrich_with_vision(
    parse_result,
    config: dict[str, Any],
    on_page: Callable[[int, int], None] | None = None,
) -> None:
    """
    Per-page Vision enrichment with fallback chain — parallel execution.

    For each page with an image:
      1. Send page image + Docling text to AI (parallel across pages)
      2. If Vision succeeds → append AI result to that page section
      3. If Vision fails → keep Docling text as-is (fallback)

    on_page: optional progress callback ``on_page(done, total)`` invoked as
        Vision pages complete (``done`` = pages finished so far, ``total`` =
        pages actually sent to Vision after triage/cap). Default None = no
        progress reporting (behaviour identical to before). Vision runs in a
        ThreadPoolExecutor, so the ``done`` counter is incremented under a lock
        (a real concurrency boundary, not defensive padding). Both the batched
        and per-page paths report through this; the report cadence is
        ``parsing.vision.progress_interval`` (default 1 = every page).

    Optional triage (parsing.vision.triage.enabled) skips pages that are
    purely text with no visual elements — saving API cost without losing info.
    Modifies parse_result.markdown in-place.

    Single-page failures stay silent (step 3): the page keeps its Docling text,
    which is the right call when Docling already had the content. But a SYSTEMIC
    failure — EVERY sent page failed AND the failed pages were content-critical
    (scanned-empty or garbled, see _is_vision_critical_page) — would otherwise
    produce a "successful" but empty/garbled knowledge base. After the per-page
    loop, a systemic-failure check (config parsing.vision.on_systemic_failure)
    decides what to do:
      - error (default): raise VisionSystemicFailure (caught at the Phase-1.5
        call site → the file is marked failed, the rest of the run continues).
      - warn: log at ERROR level, do not fail the file.
      - ignore: legacy behaviour (stay silent).
    The described/failed/critical_failed tally is written into
    parse_result.metadata["vision_triage"] so run_log / quality_report can read
    it. None of this alters the per-page fallback — failed pages still keep
    their Docling text regardless of the systemic decision.
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

    # Look up per-page structured extractions BEFORE building vision_tasks
    # (triage needs this to decide whether a page has hook data).
    structured_per_page = parse_result.metadata.get(
        "structured_extractions_per_page", {}
    )
    if not isinstance(structured_per_page, dict):
        structured_per_page = {}

    # Build vision task list with optional triage filtering + global cap
    triage_cfg = get_nested(config, "parsing.vision.triage", {})
    triage_enabled = triage_cfg.get("enabled", False)

    # Document-level language (already detected earlier in the pipeline or
    # declared via parsing.language_detection). Feeds the language-script
    # consistency check — None means "language unknown, skip that check".
    doc_language = parse_result.metadata.get("language") if parse_result.metadata else None

    # Global Vision page cap — prevents runaway API cost on huge documents.
    # null = no limit (use with caution on large documents).
    max_vision_raw = get_nested(config, "parsing.vision.max_pages", 50)
    max_vision_pages = int(max_vision_raw) if max_vision_raw is not None else None

    # xlsx sheet-level triage (partially-visual workbooks): pages belonging to
    # a sheet with ZERO drawing/ole elements show nothing beyond the cells the
    # openpyxl render already extracted (oracle-measured: their supplements
    # came back empty or pure furniture), so they skip Vision. Page ranges
    # come from xlsx_sheet_page_map (sheet → first PDF page). The map may be
    # PARTIAL — LibreOffice gives no bookmark to sheets with zero renderable
    # content — so each range must be individually anchored: a visual-less
    # sheet's pages are skipped only when the NEXT visible sheet (workbook
    # order) is also mapped and bounds the range's end (or it is the final
    # visible sheet). A neighbouring unmapped sheet's pages, if any exist,
    # could otherwise be swallowed into the range and wrongly skipped — those
    # stay on the Vision path. The all-sheets-empty case never reaches here
    # (Phase 1.3 skipped the render entirely).
    _sheet_skip_ranges: list[tuple[int, int | None]] = []  # (first, last incl; None=to end)
    if (
        get_nested(config, "parsing.xlsx.vision.sheet_triage", True)
        and parse_result.metadata.get("format") == "xlsx"
    ):
        _sv = parse_result.metadata.get("xlsx_sheet_visuals")
        _pm = parse_result.metadata.get("xlsx_sheet_page_map")
        _vis = parse_result.metadata.get("xlsx_visible_sheet_names")
        if isinstance(_sv, dict) and isinstance(_pm, dict) \
                and isinstance(_vis, list) and _vis:
            # PDF page order follows workbook sheet order; if the mapped
            # anchors aren't strictly increasing in that order something is
            # off about the map — disarm entirely (conservative).
            _anchors = [_pm[s] for s in _vis if s in _pm]
            if _anchors == sorted(set(_anchors)):
                for _i, _s in enumerate(_vis):
                    if _s not in _pm or _sv.get(_s, 1) != 0:
                        continue  # unmapped or visual (missing from scan → visual)
                    _nxt = _vis[_i + 1] if _i + 1 < len(_vis) else None
                    if _nxt is None:
                        _sheet_skip_ranges.append((_pm[_s], None))
                    elif _nxt in _pm:
                        _sheet_skip_ranges.append((_pm[_s], _pm[_nxt] - 1))
                    # else: successor unmapped → range end unanchored → send

    def _on_visualless_sheet(page_no: int) -> bool:
        return any(
            first <= page_no and (last is None or page_no <= last)
            for first, last in _sheet_skip_ranges
        )

    vision_tasks = []
    no_image = 0
    triage_skipped = 0
    furniture_skipped = 0   # subset of triage_skipped: pages skipped because
                            # their ONLY pictures were cross-page furniture
                            # (logo / header / footer). Counted separately so a
                            # run report can show how much the furniture
                            # exemption saved — and so an over-aggressive skip
                            # is traceable after the fact.
    cap_skipped = 0
    sheet_triage_skipped = 0
    pages_with_pictures = 0  # pages that carry any picture signal (would have
                             # triggered Vision pre-triage) — the "trigger" count.
    for i, page_data in enumerate(parse_result.pages):
        if not page_data.image_path:
            no_image += 1
            continue
        # Sheet-level skip — but never when a hook injected structured data
        # for this page (hooks are a public extension point, so a custom one
        # may target a sheet our drawing scan saw as empty).
        if (
            _sheet_skip_ranges
            and _on_visualless_sheet(page_data.page_no)
            and not structured_per_page.get(page_data.page_no)
        ):
            sheet_triage_skipped += 1
            continue
        if getattr(page_data, "num_pictures", 0) > 0:
            pages_with_pictures += 1
        if triage_enabled and _should_skip_vision(
            page_data, structured_per_page, triage_cfg, doc_language
        ):
            triage_skipped += 1
            # A page is a furniture skip iff it has pictures AND every picture
            # is furniture (matches _should_skip_vision's furniture_only_page
            # precondition). Cheap to recompute from PageData fields here — no
            # change to _should_skip_vision's signature / callers.
            _npic = getattr(page_data, "num_pictures", 0)
            _furn = getattr(page_data, "furniture_pic_count", 0)
            if _npic > 0 and _furn >= _npic:
                furniture_skipped += 1
            continue
        if max_vision_pages is not None and len(vision_tasks) >= max_vision_pages:
            cap_skipped += 1
            continue
        vision_tasks.append((i, page_data))

    # One-line, machine-greppable Vision triage summary — always emitted (even
    # when nothing was skipped) so a run log / -v output shows the full picture
    # at a glance: how many pages carried visuals, how many actually went to
    # Vision, how many were skipped, and how many of those skips were furniture.
    logger.info(
        f"Vision triage summary: trigger(pages w/ pictures)={pages_with_pictures}, "
        f"sent_to_vision={len(vision_tasks)}, triage_skipped={triage_skipped} "
        f"(of which furniture={furniture_skipped}), "
        f"sheet_triage_skipped={sheet_triage_skipped}, "
        f"cap_skipped={cap_skipped}, no_image_pages={no_image}"
    )
    if cap_skipped:
        logger.warning(
            f"Vision cap: {cap_skipped} page(s) skipped — "
            f"max_pages={max_vision_pages} reached. "
            f"Increase parsing.vision.max_pages or set to null to remove limit."
        )

    # Stash the triage tally on parse_result so downstream (quality report /
    # run stats) can persist it to disk — a run log line scrolls away, a JSON
    # field is there when you need to trace "why did page N skip Vision?".
    parse_result.metadata.setdefault("vision_triage", {}).update({
        "pages_with_pictures": pages_with_pictures,
        "sent_to_vision": len(vision_tasks),
        "triage_skipped": triage_skipped,
        "furniture_skipped": furniture_skipped,
        "sheet_triage_skipped": sheet_triage_skipped,
        "cap_skipped": cap_skipped,
        "no_image_pages": no_image,
    })

    doc_format = parse_result.metadata.get("format")

    # Embedded-image enrichment FIRST: figures pasted as images (docx scoring
    # rubric, xlsx screenshot sheet) lose their fine print in the shrunk
    # whole-page render. The parser already saved each figure at full
    # resolution and named its marker; this sends the above-threshold ones to
    # Vision and injects each transcription near its `<!-- image: file -->`
    # marker. Running BEFORE the page pass means supplement-mode page calls
    # receive these transcriptions as part of their ground truth — so they
    # only add what figures + body text don't already cover (annotations etc.)
    # instead of re-transcribing the figures. Also independent of vision_tasks:
    # figures must be read even when every page was triaged out.
    _enrich_embedded_images(parse_result, config, cache, doc_format)

    if not vision_tasks:
        return

    vision_timeout = get_nested(config, "models.vision.timeout_sec", 180)
    # Ceiling for the batched-Vision wall-clock cap. None = no ceiling (the
    # cap is just vision_timeout × batch_size). Separate knob because batched
    # calls are inherently longer than single-page ones; see config comment.
    vision_batch_timeout_max = get_nested(config, "models.vision.batch_timeout_max_sec", 1800)
    results: dict[int, str] = {}  # page_index → vision result text
    described = 0
    failed = 0
    # Supplement-mode pages that explicitly answered "(no additional visual
    # content)" — a SUCCESS (the ground truth already covers the page), tallied
    # separately from `failed` because `failed` feeds the systemic-failure
    # check: an all-text docx/xlsx where every page correctly says "nothing to
    # add" must not look like a total Vision outage (those pages have empty
    # page_data.text, which _is_vision_critical_page flags as critical).
    supplement_none = 0
    # Docling text of pages that FAILED Vision — kept so the systemic-failure
    # check at the end can ask _is_vision_critical_page whether the failure
    # actually cost content (scanned/garbled pages) or just a visual supplement.
    # Only populated on the failure path; success path never touches it.
    failed_page_texts: list[str] = []
    # Section indexes of pages that FAILED Vision — each gets a
    # `<!-- vision-failed page=N -->` marker written into the markdown at
    # injection time, so the failure is auditable in the artefact itself
    # (quality_report scans for it; nothing else changes for the page).
    failed_idxs: list[int] = []
    # First real error string seen on any failed page — surfaced in the
    # systemic-failure message so the user sees "API key invalid" etc. rather
    # than just a count. A 1-element list so the nested _call_vision closure can
    # write to it without a `nonlocal` declaration.
    _vision_last_error: list[str] = []

    # --- Progress reporting infrastructure (opt-in via on_page) ---
    # Vision is the longest silent stretch inside a single file (measured: a
    # 1.9 MB PDF sat 76 s with zero feedback). When on_page is provided, report
    # "done/total" as pages complete so a UI can show a live sub-progress bar.
    # done is bumped under a lock because both Vision paths run in a
    # ThreadPoolExecutor and `+= 1` is not atomic under the GIL — a real
    # concurrency boundary, not defensive padding. total = pages actually sent
    # to Vision (vision_tasks length, post-triage/cap). Cadence is configurable
    # (default 1 = every page); the last page always fires so the bar reaches
    # 100%.
    _vision_total = len(vision_tasks)
    _progress_interval = max(1, int(get_nested(config, "parsing.vision.progress_interval", 1)))
    _progress_lock = threading.Lock()
    _progress_done = [0]  # 1-element list so closures mutate without `nonlocal`

    def _report_vision_progress(units: int = 1) -> None:
        """Advance the Vision page counter by `units` and fire on_page on the
        configured cadence (and always on the final unit). No-op when on_page
        is None. Never lets a buggy callback break Vision — exceptions are
        swallowed with a warning, mirroring _emit_progress."""
        if on_page is None:
            return
        with _progress_lock:
            _progress_done[0] += units
            done = _progress_done[0]
        if done % _progress_interval == 0 or done >= _vision_total:
            try:
                on_page(done, _vision_total)
            except Exception as e:
                logger.warning(f"on_page callback raised {type(e).__name__}: {e}; ignored.")

    # ------------------------------------------------------------------
    # Batched multi-image branch — for xlsx whose single sheet renders to
    # many PDF pages. Sending all pages of one logical sheet in ONE Vision
    # call lets the model stitch cross-page table continuations (per fair
    # experiment: 90% vs 18% substring coverage and 10/10 vs 1/10
    # continuity score vs per-page). Trigger conditions are conservative:
    # only fires when the file is clearly a multi-page-per-sheet xlsx.
    # Every other format / single-page xlsx / DB-spec style xlsx (one
    # sheet ≈ one page) falls through to the per-page ThreadPoolExecutor
    # path below — that path is unchanged.
    # ------------------------------------------------------------------
    batched_cfg = get_nested(config, "parsing.vision.batched_call", {}) or {}
    batched_enabled = batched_cfg.get("enabled", False)
    min_ratio = float(batched_cfg.get("min_pages_per_sheet", 1.5))
    max_batch = int(batched_cfg.get("max_images_per_batch", 20))
    sheet_count = parse_result.metadata.get("xlsx_visible_sheet_count")

    # max_batch is now an INTRA-batch size cap (each API call sees ≤ max_batch
    # images), NOT a "skip batched if total > max_batch" gate. Earlier behaviour
    # silently fell back to per-page on long xlsx (159-page workbook had ratio
    # 12.2 ≫ 1.5 but got per-page anyway because 159 > 20). Same anti-pattern as
    # the old max_pages caps: a hardcoded ceiling silently degraded the path
    # users wanted. Now: if ratio qualifies, batched ALWAYS runs — large inputs
    # are split into N batches of ≤ max_batch each, processed serially. Per-batch
    # context stays bounded (model context limit unchanged), per-file behaviour
    # is consistent regardless of size.
    use_batched = (
        batched_enabled
        and doc_format in ("xlsx", "xls")
        and isinstance(sheet_count, int) and sheet_count > 0
        and len(vision_tasks) >= 2
        and (len(vision_tasks) / sheet_count) >= min_ratio
    )
    # Set once at the top so post-injection code can read it unconditionally.
    # Holds the stitched batched response when batched mode runs and succeeds,
    # None otherwise.
    batched_block: str | None = None

    if use_batched:
        from .parsers.vision import describe_pages_batched_cached
        from .utils.timeout import run_with_timeout

        all_image_paths = [pd.image_path for _, pd in vision_tasks]
        all_page_texts = [pd.text for _, pd in vision_tasks]
        all_page_nos = [pd.page_no for _, pd in vision_tasks]
        from .parsers.vision import resolve_supplement_only
        batched_supplement = resolve_supplement_only(config, doc_format)
        # supplement (xlsx): openpyxl already extracted the table text; feed it
        # as ground truth so Vision adds ONLY visuals, never re-transcribes the
        # table (removes xlsx duplication at the source). parse_result.markdown
        # here is the pre-Vision openpyxl render. full mode passes "" (unused).
        batched_ground_truth = parse_result.markdown if batched_supplement else ""
        # Per-batch ground-truth slicing prep: split ONCE into per-sheet
        # sections (openpyxl emits exactly one per visible sheet, and both
        # the Excel denoise and the embedded-figure injection preserve the
        # pagebreak boundaries). Slicing arms only when the section count
        # still matches the visible-sheet list — a hook that reshaped the
        # markdown breaks the alignment, so we fall back to full ground
        # truth per batch (the pre-slicing behaviour).
        _vis_names = parse_result.metadata.get("xlsx_visible_sheet_names")
        _pg_map = parse_result.metadata.get("xlsx_sheet_page_map")
        _gt_sections: list[str] | None = None
        if (
            batched_supplement
            and get_nested(config, "parsing.xlsx.vision.ground_truth_slice", True)
            and isinstance(_vis_names, list) and _vis_names
            and isinstance(_pg_map, dict) and _pg_map
        ):
            _secs: list[str] = batched_ground_truth.split(PAGEBREAK_MARKER)
            if len(_secs) == len(_vis_names):
                _gt_sections = _secs
        # Concatenate per-page structured_data so the batched prompt sees
        # the ground truth from every page in the BATCH (sliced per iteration
        # below, so each batch only sees its own pages' ground truth).
        struct_by_page: dict[int, str] = {}
        for _, pd in vision_tasks:
            sd = structured_per_page.get(pd.page_no)
            if sd:
                struct_by_page[pd.page_no] = sd

        # Split into batches of ≤ max_batch images each. ratio gate already
        # qualified the file, so we always batch — even a 159-page workbook
        # runs as 8 × 20-image batches rather than degrading to per-page.
        n_total = len(all_image_paths)
        n_batches = (n_total + max_batch - 1) // max_batch
        logger.info(
            f"Vision batched call: {n_total} page image(s) of {sheet_count} sheet(s) "
            f"(ratio {n_total/sheet_count:.1f} ≥ {min_ratio}) → "
            f"{n_batches} batch(es) of ≤ {max_batch} image(s) each"
        )

        # Run batches in parallel — controlled by performance.parallel_files
        # (same knob as per-page Vision). Earlier this was a sequential
        # for-loop, which meant a 159-page workbook split into 8 batches ran
        # 8 calls back-to-back even though each call is mostly I/O-bound
        # (Gemini Vision wait time). With parallel=4 the same workbook now
        # overlaps 4 batches at once. Failure semantics preserved: any single
        # batch failing aborts batched mode for the whole file and triggers
        # per-page fallback below — same as the old break behaviour, just
        # surfaced via cancel + flag instead of loop exit.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        parallel = get_nested(config, "performance.parallel_files", 4)

        def _run_one_batch(batch_idx: int) -> tuple[int, str | None, str | None]:
            """Returns (batch_idx, result_text, error_msg). error_msg None on success."""
            # External stop: skip batches that haven't started (their sheets
            # keep the openpyxl/Docling render; the batched path's own
            # fallback already tolerates missing batch results).
            if _EXTERNAL_STOP.is_set():
                return batch_idx, None, "stopped by user"
            start = batch_idx * max_batch
            end = min(start + max_batch, n_total)
            batch_imgs = all_image_paths[start:end]
            batch_texts = all_page_texts[start:end]
            batch_page_nos = all_page_nos[start:end]

            # Per-batch structured ground truth (only this batch's pages).
            batch_struct_parts = [
                f"[page {p}]\n{struct_by_page[p]}"
                for p in batch_page_nos if p in struct_by_page
            ]
            batch_struct = "\n\n".join(batch_struct_parts) if batch_struct_parts else None

            # Slice the ground truth to this batch's own sheets. None from the
            # slicer (any attribution doubt) → full markdown, the pre-slicing
            # behaviour.
            batch_gt = batched_ground_truth
            if _gt_sections is not None:
                sliced = _xlsx_batch_ground_truth_slice(
                    _gt_sections, _vis_names, _pg_map,
                    batch_page_nos, len(parse_result.pages),
                )
                if sliced is not None:
                    batch_gt = sliced
                    logger.debug(
                        f"batch {batch_idx + 1}/{n_batches}: ground truth sliced "
                        f"to {len(sliced):,} of {len(batched_ground_truth):,} chars"
                    )

            # Scale timeout with batch size, capped so a hung API surfaces eventually.
            # Single-batch wall-clock cap is independent of how many batches run
            # in parallel — concurrency doesn't change per-call latency.
            # Ceiling is configurable (models.vision.batch_timeout_max_sec);
            # None disables the ceiling so the cap is just timeout × batch.
            scaled = vision_timeout * len(batch_imgs)
            batch_timeout = (
                scaled if vision_batch_timeout_max is None
                else min(scaled, vision_batch_timeout_max)
            )

            try:
                with _vision_permit():
                    batch_text = run_with_timeout(
                        lambda: describe_pages_batched_cached(
                            image_paths=batch_imgs,
                            page_texts=batch_texts,
                            config=config,
                            cache=cache,
                            structured_data=batch_struct,
                            doc_format=doc_format,
                            ground_truth=batch_gt,
                        ),
                        batch_timeout,
                    )
            except TimeoutError as e:
                # Tell the caller which knob to tune: scaled=timeout×batch was
                # capped by batch_timeout_max_sec. Both fields matter — the user
                # might want to raise either timeout_sec OR the ceiling.
                hit_cap = (
                    vision_batch_timeout_max is not None
                    and scaled > vision_batch_timeout_max
                )
                hint = (
                    f"hit batch_timeout_max_sec={vision_batch_timeout_max}s "
                    f"(raise models.vision.batch_timeout_max_sec or set to null)"
                    if hit_cap
                    else f"per-call cap={vision_timeout}s × batch={len(batch_imgs)} "
                         f"(raise models.vision.timeout_sec)"
                )
                return batch_idx, None, f"TimeoutError: {e} — {hint}"
            except Exception as e:
                return batch_idx, None, f"{type(e).__name__}: {e}"
            if not (batch_text and batch_text.strip()):
                return batch_idx, None, "empty response"
            if batched_supplement and _is_empty_supplement(batch_text):
                # supplement found nothing visual to add for this batch — normal,
                # NOT a failure; emit no block (the openpyxl table text stands).
                return batch_idx, "", None

            page_range = f"{batch_page_nos[0]}-{batch_page_nos[-1]}"
            wrapped = (
                f"<!-- vision-enriched batched batch={batch_idx + 1}/{n_batches} "
                f"pages={page_range} -->\n{batch_text.strip()}"
            )
            return batch_idx, wrapped, None

        results_by_idx: dict[int, str] = {}
        any_batch_failed = False
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(_run_one_batch, idx): idx for idx in range(n_batches)}
            for future in as_completed(futures):
                idx, wrapped, err = future.result()
                if err is not None:
                    logger.warning(
                        f"Vision batched call failed on batch {idx + 1}/{n_batches} "
                        f"({err}) — falling back to per-page Vision for the WHOLE "
                        f"file (no partial-batched output)."
                    )
                    any_batch_failed = True
                    # Cancel remaining futures; they may still execute if
                    # already started (Python can't preempt mid-call), but
                    # this prevents NEW submissions and lets the executor
                    # exit faster.
                    for f in futures:
                        f.cancel()
                    break
                # wrapped is guaranteed non-None when err is None (see _run_one_batch).
                assert wrapped is not None
                results_by_idx[idx] = wrapped
                # Sub-progress: advance by this batch's page count (last batch
                # may be short). total = n_total = len(vision_tasks), so the
                # batched and per-page paths report the same "Vision pages
                # done / total" semantics to the UI.
                _batch_pages = min(max_batch, n_total - idx * max_batch)
                _report_vision_progress(_batch_pages)
                # Approx progress signal — order is "completion order" not
                # idx order, which matches what users expect for parallel work.
                logger.info(
                    f"  batch {idx + 1}/{n_batches}: described "
                    f"({len(wrapped):,} chars)"
                )

        if any_batch_failed:
            use_batched = False
            # Batched mode aborted → the per-page branch below re-processes the
            # SAME pages from scratch. Reset the progress counter so the UI
            # bar restarts from 0 instead of double-counting the partial
            # batched run on top of the full per-page run.
            with _progress_lock:
                _progress_done[0] = 0
        else:
            # Successful batched run: merge by ORIGINAL batch order (idx) so
            # the assembled markdown preserves source page sequence even
            # though batches completed out of order.
            described = n_total
            batched_blocks = [results_by_idx[i] for i in range(n_batches)]
            batched_block = "\n\n" + "\n\n".join(batched_blocks) + "\n"

    # ------------------------------------------------------------------
    # Per-page parallel branch — original behaviour. Runs when batched
    # mode is disabled, doesn't apply, or fell back due to failure.
    # ------------------------------------------------------------------
    if not use_batched:
        parallel = get_nested(config, "performance.parallel_files", 4)
        # Per-page supplement fix: xlsx that renders 1-sheet≈1-page goes per-page
        # (not batched). page_data.text is EMPTY for LibreOffice-rendered xlsx
        # pages → the supplement prompt's "Docling empty → transcribe full" escape
        # fires → Vision re-transcribes the whole table (measured: moonmile dup
        # 37% unchanged). Feed the openpyxl render (parse_result.markdown is
        # pre-Vision here = the openpyxl table text) as the per-page Docling text
        # so supplement knows the table is already captured. full mode (PDF/PPT)
        # keeps the page's own text. Mirrors the batched-path ground_truth fix.
        # docx (supplement via parsing.docx.vision.supplement_only) rides the
        # same path: its LibreOffice pages also have empty page_data.text, and
        # parse_result.markdown here already contains the embedded-figure
        # transcriptions (injected before this pass) — so page calls only add
        # what body text + figures don't cover (e.g. Word-layer annotations).
        from .parsers.vision import resolve_supplement_only
        _pp_supplement = resolve_supplement_only(config, doc_format)
        _pp_ground = parse_result.markdown if _pp_supplement else None
        # Per-page ground-truth slicing — same cost fix as the batched path
        # (each call gets its own sheet's text, not the whole workbook).
        # Arms on detection, not format: docx has no sheet→page map, so the
        # helper returns {} and every page falls back to the full markdown
        # (the pre-slicing behaviour). Same config switch as batched —
        # batched/per-page is an implementation detail, not a user concern.
        _pp_gt_by_page: dict[int, str] = {}
        if _pp_ground is not None:
            if get_nested(
                config, "parsing.xlsx.vision.ground_truth_slice", True
            ):
                _pp_gt_by_page = _xlsx_per_page_ground_truth(
                    _pp_ground,
                    parse_result.metadata.get("xlsx_visible_sheet_names"),
                    parse_result.metadata.get("xlsx_sheet_page_map"),
                    [pd.page_no for _, pd in vision_tasks],
                    len(parse_result.pages),
                )
            # docx variant: slices via the LibreOffice PDF text layer
            # (Step 1.5b) instead of a sheet map. Detection-ordered, not
            # format-branched: xlsx has no docx_page_texts and docx has no
            # sheet map, so exactly one (or neither) arms.
            if not _pp_gt_by_page and get_nested(
                config, "parsing.docx.vision.ground_truth_slice", True
            ):
                _pp_gt_by_page = _docx_per_page_ground_truth(
                    _pp_ground,
                    parse_result.metadata.get("docx_page_texts"),
                    [pd.page_no for _, pd in vision_tasks],
                )
            if _pp_gt_by_page:
                logger.debug(
                    f"per-page ground truth sliced for "
                    f"{len(_pp_gt_by_page)}/{len(vision_tasks)} page(s)"
                )

        def _call_vision(idx: int, page_data) -> tuple[int, str | None]:
            from .utils.timeout import run_with_timeout
            # External stop: pages whose worker hasn't started yet bail out
            # instantly (the page keeps its Docling text); only the few calls
            # already in flight run to completion. This is what lets a stop
            # land in seconds on a 90-page file instead of minutes.
            if _EXTERNAL_STOP.is_set():
                return idx, None
            try:
                # page_data.page_no is 1-based and matches the hook's
                # convention for populating structured_extractions_per_page.
                struct_data = structured_per_page.get(page_data.page_no)
                _g = _pp_ground  # local rebind so the narrow holds inside the closure
                pg_text = (
                    _pp_gt_by_page.get(int(page_data.page_no), _g)
                    if _g is not None
                    else page_data.text
                )
                with _vision_permit():
                    return idx, run_with_timeout(
                        lambda: describe_page_cached(
                            image_path=page_data.image_path,
                            page_text=pg_text,
                            config=config,
                            cache=cache,
                            structured_data=struct_data,
                            doc_format=doc_format,
                        ),
                        vision_timeout,
                    )
            except TimeoutError as e:
                logger.warning(
                    f"Vision timed out for page {page_data.page_no}: {e} "
                    f"(per-call cap={vision_timeout}s — raise "
                    f"models.vision.timeout_sec for slow endpoints / dense pages)"
                )
                if not _vision_last_error:
                    _vision_last_error.append(
                        f"timeout: {e} — raise models.vision.timeout_sec "
                        f"(current={vision_timeout}s)"
                    )
                return idx, None
            except Exception as e:
                logger.warning(f"Vision failed for page {page_data.page_no}: {e}")
                if not _vision_last_error:
                    _vision_last_error.append(f"{type(e).__name__}: {e}")
                return idx, None

        # idx → that page's Docling text, so a failed page can be judged
        # critical-or-not by the systemic-failure check below. Built from
        # vision_tasks (the same (idx, page_data) pairs submitted as futures).
        _idx_to_text = {idx: pd.text for idx, pd in vision_tasks}

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_call_vision, idx, pd): idx
                for idx, pd in vision_tasks
            }
            for future in as_completed(futures):
                idx, result_text = future.result()
                cleaned = result_text.strip() if result_text else ""
                if cleaned and not _is_empty_supplement(cleaned):
                    results[idx] = cleaned
                    described += 1
                elif cleaned:
                    # Supplement mode: "(no additional visual content)" means
                    # the ground truth already captured the page — emit NO
                    # vision-enriched block (that's the whole point of dedup),
                    # and count it as a clean outcome, not a failure (see
                    # supplement_none above).
                    supplement_none += 1
                else:
                    failed += 1
                    # Record this page's Docling text for the systemic-failure
                    # check. Does NOT alter the fallback — the page still keeps
                    # its Docling text as before; we only remember it failed.
                    failed_page_texts.append(_idx_to_text.get(idx, ""))
                    failed_idxs.append(idx)
                # Sub-progress: one page done (success or fail both count — the
                # bar tracks Vision *completion*, not success rate).
                _report_vision_progress(1)

    # Supplement-mode outputs carry figure-internal headings (chart titles,
    # axis legends) that must not enter the document's heading hierarchy —
    # demote to bold before injection (see _demote_headings). Full-mode
    # pages (PDF/PPT) keep their headings: there Vision's structure IS the
    # page's structure (it reconstructs what Docling fragmented).
    from .parsers.vision import resolve_supplement_only as _rso
    if _rso(config, doc_format) and get_nested(
        config, "parsing.vision.supplement_demote_headings", True
    ):
        results = {idx: _demote_headings(t) for idx, t in results.items()}
        if batched_block:
            batched_block = _demote_headings(batched_block)

    # Inject results into markdown sections.
    # Two modes depending on whether the source has pagebreak markers:
    #   A) pagebreak-aligned (PDF, PPT): inject each page's result into its section
    #   B) no pagebreaks (DOCX via LibreOffice): append all results in page order
    #      at the end of the document. We can't align to text sections because
    #      LibreOffice-rendered pages don't correspond to Docling's text layout.
    pagebreak = PAGEBREAK_MARKER
    sections = parse_result.markdown.split(pagebreak)
    has_pagebreaks = len(sections) > 1

    if has_pagebreaks:
        # Mode A: align by section index.
        # Overflow: when LibreOffice renders one xlsx sheet to multiple PDF
        # pages (long 方眼紙 layouts), page_count can exceed sheet_count and
        # results[idx>=len(sections)] would otherwise be silently dropped.
        # Append overflow to the last section (typically continuation pages
        # of the final sheet) and log a warning so the leak is visible.
        overflow: list[tuple[int, str]] = []
        for idx, text in results.items():
            if idx < len(sections):
                sections[idx] = (
                    sections[idx].rstrip()
                    + f"\n\n<!-- vision-enriched -->\n{text}\n"
                )
            else:
                overflow.append((idx, text))
        if overflow:
            overflow.sort()
            extra = "\n\n".join(
                f"<!-- vision-enriched page={idx + 1} (overflow) -->\n{text}"
                for idx, text in overflow
            )
            sections[-1] = sections[-1].rstrip() + f"\n\n{extra}\n"
            logger.warning(
                f"Vision: {len(overflow)} page result(s) overflowed pagebreak "
                f"count ({len(sections)} sections vs {len(results)} pages) — "
                f"appended to last section. Common with xlsx whose LibreOffice "
                f"render produces more pages than sheets. "
                f"Overflow chunks inherit the last section's title_path, which "
                f"misattributes pages originally belonging to earlier sheets. "
                f"If precise per-sheet attribution matters for your RAG, filter "
                f"by the '(overflow)' marker in chunk text rather than title_path."
            )
        # Failed pages: a machine-scannable marker in the page's own section
        # (overflow indexes land in the last section, same rule as results).
        # Same philosophy as [unreadable]: the artefact records its own gaps,
        # quality_report stays a pure disk scanner.
        for idx in sorted(failed_idxs):
            sec = idx if idx < len(sections) else -1
            sections[sec] = (
                sections[sec].rstrip() + f"\n\n<!-- vision-failed page={idx + 1} -->\n"
            )
        # Batched response (when batched mode succeeded): append to the
        # last section. The block already carries its own `(batched, pages=X-Y)`
        # marker so downstream consumers can identify and filter it.
        if batched_block:
            sections[-1] = sections[-1].rstrip() + batched_block
        parse_result.markdown = pagebreak.join(sections)
    else:
        # Mode B: append all Vision results in page order at end of document
        # Batched block (when in Mode B + batched mode succeeded) is also
        # appended here as a single trailing section.
        if batched_block:
            parse_result.markdown = (
                parse_result.markdown.rstrip() + batched_block
            )
        if results:
            ordered = sorted(results.items())
            appended = "\n\n".join(
                f"<!-- vision-enriched page={idx + 1} -->\n{text}"
                for idx, text in ordered
            )
            parse_result.markdown = (
                parse_result.markdown.rstrip()
                + "\n\n"
                + appended
                + "\n"
            )
        if failed_idxs:
            # Failed pages, Mode B: trailing markers in page order — same
            # auditability as Mode A, same place the page results would land.
            fail_lines = "\n".join(
                f"<!-- vision-failed page={idx + 1} -->"
                for idx in sorted(failed_idxs)
            )
            parse_result.markdown = (
                parse_result.markdown.rstrip() + "\n\n" + fail_lines + "\n"
            )

    if cache_enabled:
        cache.close()

    mode_tag = "batched" if use_batched else f"parallel={get_nested(config, 'performance.parallel_files', 4)}"
    logger.info(
        f"Vision enrichment: {described} described, "
        f"{supplement_none} no-supplement, {failed} failed, "
        f"{no_image} no-image, {triage_skipped} triage-skipped, "
        f"{cap_skipped} cap-skipped ({mode_tag})"
    )

    # Lineage record — only when at least one page was actually enriched.
    # Pages triaged-out or failed are intentionally excluded: transformations
    # is a positive provenance trail, not a failure log.
    if described > 0:
        pages_enriched = sorted(
            parse_result.pages[idx].page_no for idx in results.keys()
            if idx < len(parse_result.pages)
        )
        vision_model = get_nested(
            config, "models.vision.primary.model", "unknown"
        )
        parse_result.transformations.append({
            "step": "vision",
            "model": vision_model,
            "pages_enriched": pages_enriched,
        })

    # ------------------------------------------------------------------
    # Systemic-failure check — distinguish "all Vision failed and it cost us
    # real content" (fail loud) from "a page failed but Docling already had the
    # text" (ordinary fallback, already handled per-page above). This NEVER
    # touches the per-page fallback: every failed page still kept its Docling
    # text. It only decides whether to ALSO surface the total failure.
    #
    # Two signals must BOTH hold to act:
    #   A (scale):     described == 0 and failed > 0  — every sent page failed.
    #   B (criticality): >= min_critical_pages of those failed pages were
    #                    content-critical (scanned-empty or garbled — see
    #                    _is_vision_critical_page). Pages whose Docling text was
    #                    clean (Vision was only supplementing visuals) do NOT
    #                    count, so a clean text PDF whose Vision happens to fail
    #                    stays successful.
    # ------------------------------------------------------------------
    # External stop exemption: a user stop makes every not-yet-started page
    # "fail" by design — that is an interrupt, not a systemic API failure.
    # Without this, stopping a scanned document would raise a bogus
    # VisionSystemicFailure on top of the stop.
    if _EXTERNAL_STOP.is_set():
        failed_page_texts = []

    sysf_cfg = get_nested(config, "parsing.vision.systemic_failure", {}) or {}
    if not isinstance(sysf_cfg, dict):
        sysf_cfg = {}
    critical_failed = sum(
        1 for t in failed_page_texts
        if _is_vision_critical_page(t, triage_cfg, sysf_cfg)
    )
    # Persist the failure tally alongside the triage tally so run_log /
    # quality_report can read it (FileResult.vision_triage keeps only int
    # values, so these three ints carry through). Previously described/failed
    # were log-only and never reached disk.
    parse_result.metadata.setdefault("vision_triage", {}).update({
        "described": described,
        "supplement_none": supplement_none,
        "failed": failed,
        "critical_failed": critical_failed,
    })

    on_systemic = str(
        get_nested(config, "parsing.vision.on_systemic_failure", "error")
    ).lower()
    min_critical = int(sysf_cfg.get("min_critical_pages", 1))

    systemic = (
        described == 0
        and failed > 0
        and critical_failed >= min_critical
        and on_systemic != "ignore"
    )
    if systemic:
        first_err = _vision_last_error[0] if _vision_last_error else "unknown"
        msg = (
            f"Systemic Vision failure: {failed} page(s) sent, 0 described, "
            f"{critical_failed} content-critical page(s) have NO usable "
            f"Docling text and depended on Vision (scanned / garbled). The "
            f"knowledge base for this file would be empty/garbled. "
            f"First error: {first_err}"
        )
        if on_systemic == "warn":
            # Do not fail the file; just make the failure impossible to miss.
            logger.error("%s — continuing (on_systemic_failure=warn)", msg)
        else:  # "error" (default) and any unrecognised value → fail loud
            raise VisionSystemicFailure(msg)


# ---------------------------------------------------------------------------
# Docling + Vision deduplication
# ---------------------------------------------------------------------------

# Matches every vision-enriched marker variant: plain `<!-- vision-enriched -->`,
# `... page=N`, `... page=N (overflow)`, and `... batched batch=X/Y pages=A-B`.
_VISION_MARK_RE = re.compile(r"<!-- vision-enriched[^>]*-->")


def _apply_vision_keep(markdown: str, config: dict[str, Any], doc_format: str | None) -> str:
    """
    De-duplicate Docling↔Vision overlap by KEEPING ONE HALF — chosen by config,
    split on the `<!-- vision-enriched ... -->` marker (never by length-ratio
    guessing, which silently overwrote the wrong side in the old dedup).

    output.vision_keep:
      both    → return markdown unchanged (DEFAULT, zero content loss)
      vision  → drop the Docling half (before the marker), keep the marker + Vision
      docling → drop the marker + Vision half, keep the Docling half

    SAFETY — page-aligned, full-mode formats only:
      - Supplement-mode formats (xlsx): Vision only supplements visuals; the
        table body lives in the Docling/openpyxl half. Dropping Docling would
        delete the table body → we force `both` for these formats.
      - Mode B flow formats (docx / doc / html / md / txt): rendered via
        LibreOffice with no native pagination, so Vision is appended at the
        document end and does NOT align with the Docling text — the two halves
        are different content, not a superset pair → force `both`. Note we key
        this off the FORMAT, not "markdown has no pagebreak marker": a single-
        page PDF also has no pagebreak yet IS a clean Docling/Vision superset
        pair, so it must still be de-duped.

    Frontmatter (a leading `---\\n...\\n---` block) is always preserved.
    Sections without a vision marker are returned untouched.
    """
    keep = str(get_nested(config, "output.vision_keep", "both")).lower()
    if keep == "both" or keep not in ("vision", "docling"):
        return markdown

    pagebreak = PAGEBREAK_MARKER

    # Guard 1: supplement-mode formats — Vision is additive, Docling holds the
    # body. Dropping Docling there is data loss, so leave both halves intact.
    from .parsers.vision import resolve_supplement_only
    if resolve_supplement_only(config, doc_format):
        return markdown

    # Guard 2: Mode B flow formats — Vision appended at the end, not aligned to
    # Docling text. Keyed off format (not pagebreak presence): a single-page PDF
    # also lacks a pagebreak but IS a page-aligned superset pair, so de-dup it.
    _MODE_B_FORMATS = {"docx", "doc", "html", "htm", "md", "markdown", "txt"}
    if (doc_format or "").lower() in _MODE_B_FORMATS:
        return markdown

    out: list[str] = []
    for section in markdown.split(pagebreak):
        m = _VISION_MARK_RE.search(section)
        if not m:
            out.append(section)
            continue

        # Preserve a leading frontmatter block (only possible in the first
        # section) regardless of which half we drop.
        frontmatter = ""
        body = section
        if section.startswith("---\n") and "\n---" in section[4:]:
            fm_end = section.index("\n---", 4) + 4
            frontmatter = section[:fm_end]
            body = section[fm_end:]
            m = _VISION_MARK_RE.search(body)
            if not m:
                out.append(section)
                continue

        if keep == "vision":
            # Keep the marker + everything after it (Vision); drop Docling before.
            kept = body[m.start():]
            out.append(frontmatter + "\n\n" + kept if frontmatter else kept)
        else:  # keep == "docling"
            # Keep everything before the marker (Docling); drop marker + Vision.
            kept = body[:m.start()].rstrip()
            out.append(frontmatter + kept if frontmatter else kept)

    return pagebreak.join(out)


# ---------------------------------------------------------------------------
# Chunk lineage — attach provenance trail to each chunk
# ---------------------------------------------------------------------------

def _build_chunk_lineage(
    parse_result,
    chunker,
    original_file: Path,
    source_md_rel: str,
) -> dict[str, Any]:
    """
    Build a lineage dict to attach to every chunk's metadata.

    Structure — two parts:

    1. SOURCES — where this chunk's content came from:
       * source_markdown: the sources/*.md path the chunk was cut from.
       * original_input:  the raw input file (PDF / DOCX / ... or URL).

    2. TRANSFORMATIONS — ordered record of what shaped the chunk:
       parser → pre-parse hook → post-parse hooks → vision → chunker.
       Each entry carries its own schema; consumers filter by `step`.

    Keeping this as a single sub-dict under `metadata.lineage` avoids
    polluting the flat metadata namespace (chunk_index / tokens / etc.)
    and gives RAG downstreams one obvious field to consult for citation
    / quality attribution / reproducibility.

    The function is intentionally additive: existing flat metadata keys
    (source / original_file / format / language / title_path) are NOT
    moved or removed. Downstream consumers reading those keys are not
    affected.
    """
    original_input: dict[str, Any] = {"filename": original_file.name}
    # Mimetype + hash come from docling's origin info when the parser is
    # Docling; media / text parsers skip these. Mirror the existing flat
    # fields rather than re-introspecting the file.
    meta = parse_result.metadata or {}
    origin = meta.get("docling_origin") or {}
    if isinstance(origin, dict):
        if origin.get("mimetype"):
            original_input["mimetype"] = origin["mimetype"]
        if origin.get("binary_hash") is not None:
            original_input["binary_hash"] = origin["binary_hash"]
    # last_modified was stamped onto doc_metadata earlier in process_single_file;
    # we pull from there (via parse_result.metadata) so the lineage stays in sync
    # with what the chunker's doc_metadata already carries.
    if meta.get("last_modified"):
        original_input["last_modified"] = meta["last_modified"]

    # Copy transformations so later chunks can't accidentally mutate the
    # parse_result's list (e.g. if post-processing logic ever appends).
    transformations: list[dict[str, Any]] = list(parse_result.transformations or [])

    # Add the chunker step LAST in provenance order. We read the chunker's
    # public attrs directly — strategy name comes from class name, primary
    # knobs come from BaseChunker's resolved _max_tokens / _min_tokens so
    # the numbers match what actually ran (not the raw config values,
    # which may carry defaults we don't want to stamp).
    if chunker is not None:
        chunker_entry: dict[str, Any] = {
            "step": "chunker",
            "name": chunker.__class__.__name__,
        }
        max_tokens = getattr(chunker, "_max_tokens", None)
        if max_tokens is not None:
            chunker_entry["max_tokens"] = max_tokens
        # Concrete strategy is visible via class name; auto chunkers that
        # dispatched internally record the dispatched name in their metadata
        # (slide/sheet/heading/recursive/...) — we rely on that convention.
        transformations.append(chunker_entry)

    return {
        "source_markdown": source_md_rel,
        "original_input": original_input,
        "transformations": transformations,
    }


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

    # Step 2: Merge fragments into adjacent chunks (bidirectional)
    # First pass: try merging backward (fragment → previous chunk, same section)
    after_backward: list = []
    for chunk in chunks:
        if _is_fragment(chunk, min_tokens) and after_backward:
            if _same_section(after_backward[-1], chunk):
                after_backward[-1].text = after_backward[-1].text + "\n\n" + chunk.text
                continue
        after_backward.append(chunk)

    # Second pass: merge remaining fragments forward (fragment → next chunk)
    # This catches fragments whose previous chunk is a different section
    # but whose next chunk is the same section (e.g. heading-only chunk
    # that starts a new section, followed by the section's content).
    merged: list = []
    i = 0
    while i < len(after_backward):
        chunk = after_backward[i]
        if _is_fragment(chunk, min_tokens) and i + 1 < len(after_backward):
            next_chunk = after_backward[i + 1]
            if _same_section(chunk, next_chunk):
                next_chunk.text = chunk.text + "\n\n" + next_chunk.text
                # Skip this fragment — its content is now in next_chunk
                i += 1
                continue
        merged.append(chunk)
        i += 1

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

def _probe_page_count(file_path: Path) -> int | None:
    """Cheap page-count probe for sizing the parse timeout (PDF only).

    PyMuPDF reads the count from the xref in O(1) (~ms even on 1000-page
    files). Returns None for non-PDFs or any failure — the caller then falls
    back to the fixed timeout. Never raises: a failed probe must not block
    the parse it's only trying to budget.
    """
    if file_path.suffix.lower() != ".pdf":
        return None
    try:
        import pymupdf
        with pymupdf.open(str(file_path)) as doc:
            return len(doc)
    except Exception:
        return None


def _resolve_parse_timeout(file_path: Path, config: dict[str, Any]) -> float | None:
    """Resolve the wall-clock parse timeout for one file.

    With parsing.dynamic_timeout.enabled (default), paged inputs get a budget
    scaled to their page count: clamp(base + per_page * pages, base, max).
    Falls back to the fixed parsing.timeout_sec when dynamic sizing is off or
    the page count can't be probed (non-PDF / probe failure). Returns None
    (= no timeout) only when the fixed value itself is null.
    """
    fixed = get_nested(config, "parsing.timeout_sec", 600)
    if not get_nested(config, "parsing.dynamic_timeout.enabled", True):
        return fixed
    pages = _probe_page_count(file_path)
    if not pages or pages <= 0:
        return fixed
    base = float(get_nested(config, "parsing.dynamic_timeout.base_sec", 120))
    per_page = float(get_nested(config, "parsing.dynamic_timeout.per_page_sec", 3))
    ceiling = float(get_nested(config, "parsing.dynamic_timeout.max_sec", 1800))
    return max(base, min(ceiling, base + per_page * pages))


def process_single_file(
    file_path: Path,
    parser: BaseParser,
    chunker: BaseChunker | None,
    config: dict[str, Any],
    output_dir: Path,
    existing_names: set[str] | None = None,
    on_file_progress: Callable[[str, int, int], None] | None = None,
) -> tuple[FileResult, list]:
    """
    Process a single file through Phase 1 → 2 → 3.

    Args:
        file_path: Input file path.
        parser: Parser instance to use.
        chunker: Chunker instance (None if chunking disabled).
        config: Full config dict.
        output_dir: Base output directory (e.g. ./knowledge/).
        on_file_progress: Optional within-file progress callback
            ``on_file_progress(phase, done, total)``. Currently fired by the
            Vision phase (phase="vision") as pages complete, so a UI can show
            sub-file progress during the longest silent stretch. Default None =
            no within-file reporting (behaviour identical to before).

    Returns:
        FileResult with processing details.
    """
    result = FileResult(original_file=str(file_path))

    # --- Phase 0.5: Legacy format convert (.xls/.doc/.ppt → OOXML via LibreOffice) ---
    # When this fires, `file_path` is rebound to the cached modern file so every
    # downstream Phase (hooks / parser / page-image / metadata) flows
    # through the modern-format path unchanged. The ORIGINAL legacy path is held
    # in `result.original_file` (set above) and reused for lineage below, so
    # `metadata.lineage.original_input` still points at the user's input.
    legacy_convert_record: dict[str, Any] | None = None
    file_path, legacy_convert_record = _maybe_convert_legacy_office(
        file_path, config, output_dir,
    )

    # --- Phase 1.0: Pre-parse hooks (e.g. DOCX OMML → LaTeX preprocessing) ---
    # Hooks can return a BytesIO stream that replaces the file content before
    # Docling sees it. None means "use original file". Hooks never raise —
    # failures degrade to the original file with a warning.
    # The second return value is the name of the winning hook (if any) —
    # appended to transformations further down, after parsing succeeds,
    # so provenance reflects actually-used enrichments only.
    override_stream, pre_parse_hook_name = run_pre_parse_hooks(file_path, config)

    # --- Phase 1: Parse ---
    t0 = time.monotonic()
    # Size-aware timeout: a 500-page book gets a budget scaled to its page
    # count instead of the same flat cap a 10-page memo gets (see
    # _resolve_parse_timeout / parsing.dynamic_timeout).
    parse_timeout = _resolve_parse_timeout(file_path, config)
    # Parse is a Docling black box (no per-page hook), so we can only signal
    # "busy parsing" — not a percentage. Emitting sub_total=0 tells the UI to
    # show an indeterminate/spinner state for this phase instead of sitting
    # frozen for the ~30 s a large PDF takes before Vision starts.
    if on_file_progress is not None:
        on_file_progress("parse", 0, 0)
    try:
        from .utils.timeout import run_with_timeout
        # _PARSE_GATE: parsing is globally exclusive (see the gate's comment
        # at module top). Lock OUTSIDE the timeout wrapper so the budget
        # covers parsing only, never time spent queueing behind another file.
        with _PARSE_GATE:
            parse_result = run_with_timeout(
                lambda: parser.parse(file_path, override_stream=override_stream),
                parse_timeout,
            )
    except TimeoutError as e:
        result.success = False
        # Tell the caller exactly which knob to raise — both humans and agents
        # see the error string, and "timed out after Ns" alone doesn't reveal
        # whether to raise parsing.timeout_sec or parsing.dynamic_timeout.max_sec.
        ext = file_path.suffix.lstrip(".").lower()
        dyn_on = get_nested(config, "parsing.dynamic_timeout.enabled", True)
        is_paged_pdf = ext == "pdf" and dyn_on
        knob = (
            "parsing.dynamic_timeout.max_sec" if is_paged_pdf
            else "parsing.timeout_sec"
        )
        env_knob = "DOCINGEST__" + knob.replace(".", "__")
        result.error = (
            f"Parse timed out: {e}. "
            f"This is {knob} (current={parse_timeout}s). "
            f"Raise it for this input — e.g. {env_knob}=1800, or set in your "
            f"docingest.yaml. For long-form video, native_video uploads can "
            f"also stall on Files API: see parsing.audio.native_video.files_api_poll_timeout_sec."
        )
        result.error_type = "timeout"
        return result, []
    except (FileNotFoundError, PermissionError, OSError) as e:
        result.success = False
        result.error = f"Parse failed (io): {e}"
        result.error_type = "io_error"
        return result, []
    except Exception as e:
        result.success = False
        # A parse can fail because the file is password-protected; surface that
        # as a clear, actionable reason instead of the raw parser error. Checked
        # only here on the failure path, so successful files pay nothing.
        enc_reason = _detect_encrypted_reason(file_path)
        if enc_reason:
            result.error = enc_reason
            result.error_type = "encrypted"
        else:
            result.error = f"Parse failed: {e}"
            result.error_type = "parse_error"
        return result, []
    result.parse_time_ms = int((time.monotonic() - t0) * 1000)

    if not parse_result.success:
        # Check error handling config. Either way the file is marked failed;
        # the only difference is whether we stop the run (fail) or keep going
        # (skip). The error message/type are resolved the same way for both.
        result.success = False
        enc_reason = _detect_encrypted_reason(file_path)
        if enc_reason:
            result.error = enc_reason
            result.error_type = "encrypted"
        else:
            result.error = parse_result.error
            result.error_type = "parse_error"
        return result, []

    result.format = parse_result.metadata.get("format", "unknown")

    # Lineage record — provenance order is the order things ran:
    #   format_convert (Phase 0.5) → pre_parse hook (Phase 1.0) → parser.
    # legacy_convert_record is prepended so the lineage trail starts at the
    # user's original legacy input, then shows the conversion, then the
    # rest of the pipeline operating on the converted modern file.
    if legacy_convert_record is not None:
        parse_result.transformations.insert(0, legacy_convert_record)
    if pre_parse_hook_name:
        parse_result.transformations.append({
            "step": "hook",
            "name": pre_parse_hook_name,
            "phase": "pre_parse",
        })
    # parser.__class__.__name__ is stable and human-readable ("DoclingParser"
    # / "MediaParser" / "TextParser"), which is exactly what a consumer
    # wants to see in provenance trails.
    parse_result.transformations.append({
        "step": "parser",
        "name": parser.__class__.__name__,
        "format": result.format,
    })

    # Phase 0.5 aftermath: when a legacy file was auto-converted upstream, the
    # parser saw the cached file (stem = sha256). Restore the user's original
    # stem so derived metadata (frontmatter title, derive_aliases_hook output)
    # reflects the input file. Done BEFORE Phase 1.6 pre_write hooks so
    # derive_aliases_hook reads the corrected title and produces clean aliases.
    if legacy_convert_record is not None:
        parse_result.metadata["title"] = Path(result.original_file).stem

    # --- Phase 1.1: Garbled text detection + pymupdf fallback ---
    if parse_result.markdown and _detect_garbled(parse_result.markdown):
        _pipeline_logger.warning(
            f"Garbled text detected in Docling output for {file_path.name}, "
            f"attempting pymupdf fallback"
        )
        _pymupdf_fallback(file_path, parse_result)

    # --- Phase 1.2: Excel denoising (all xlsx/xls/csv, unified path) ---
    if result.format in ("xlsx", "xls", "csv") and parse_result.markdown:
        parse_result.markdown = _clean_excel_markdown(
            parse_result.markdown, config,
        )

    # --- Phase 1.2.5: Generic Markdown table-row denoising (all other formats) ---
    # Docling expands merged table cells by repeating the same value across
    # every column a merge spans. A single PDF cell spanning 10 columns becomes
    # `| foo | foo | foo | foo | foo | foo | foo | foo | foo | foo |` — 10×
    # the token cost with zero extra information. The xlsx path above already
    # handles this; this block applies the same row-level dedup to PDF / DOCX
    # / PPTX / HTML where Docling's Markdown output has the same artefact.
    #
    # Gated by its own config key (parsing.markdown.dedup_table_rows, default
    # true) so any user who genuinely wants Docling's raw output can disable
    # it without touching the xlsx path.
    elif (
        parse_result.markdown
        and get_nested(config, "parsing.markdown.dedup_table_rows", True)
    ):
        parse_result.markdown = _denoise_markdown_table_rows(parse_result.markdown)

    # --- Phase 1.3: Ensure page images for Vision (formats without native page rendering) ---
    # All three Office formats route through the same LibreOffice → PDF → screenshots
    # backend. Each has its own config section so limits can be tuned per format.
    #
    # Gated on parsing.vision.enabled: these page images exist ONLY to feed
    # per-page Vision (Phase 1.5). With Vision off, rendering them via
    # LibreOffice is pure wasted work — and it's expensive (a single PPTX
    # measured 8.3 s of LibreOffice rendering). This is the biggest instance of
    # the "render images nobody consumes" trap because EVERY Office file hits
    # Phase 1.3, unlike the PDF fallback which only fires when Docling produced
    # no page image. Vision-off integrators (e.g. markdown-only callers) paid
    # this on every Office document before this guard.
    if get_nested(config, "parsing.vision.enabled", True):
        if result.format in ("xlsx", "xls"):
            if _office_file_has_no_visuals(parse_result, config, "xlsx"):
                _pipeline_logger.info(
                    f"{file_path.name}: no visual elements on any visible sheet "
                    f"— skipping LibreOffice render + page Vision "
                    f"(parsing.xlsx.vision.sheet_triage)"
                )
            else:
                _ensure_excel_page_images(file_path, parse_result, config)
        elif result.format in ("docx", "doc"):
            if _office_file_has_no_visuals(parse_result, config, "docx"):
                _pipeline_logger.info(
                    f"{file_path.name}: text-only body (no drawing/ink/highlight"
                    f"/tracked-change signals) — skipping LibreOffice render + "
                    f"page Vision (parsing.docx.vision.skip_text_only)"
                )
            else:
                _ensure_docx_page_images(file_path, parse_result, config)
        elif result.format in ("pptx", "ppt"):
            _ensure_pptx_page_images(file_path, parse_result, config)

    # --- Phase 1.4: Post-parse hooks (pre-Vision) ---
    # Hooks that inject structured data the Vision step should be aware of
    # (e.g. PPTX chart data read directly from python-pptx). Runs before
    # Vision so the prompt can reference already-extracted data.
    run_post_parse_hooks(file_path, parse_result, config, phase="post_parse")

    # Detect language BEFORE Vision so the triage step can use it (the
    # language-script consistency check in Vision triage needs to know the
    # declared language to whitelist expected scripts). For a document whose
    # first page is garbled but the rest is clean Japanese, the full-document
    # detection still correctly yields "ja" — exactly the signal the
    # per-page script check needs.
    if "language" not in parse_result.metadata:
        parse_result.metadata["language"] = _detect_language(parse_result.markdown)

    # --- Phase 1.5: Vision enrichment (describe extracted images) ---
    if parse_result.pages and get_nested(config, "parsing.vision.enabled", True):
        try:
            # Wrap the file-level callback into the (done, total) shape
            # _enrich_with_vision expects, tagging the phase as "vision".
            _vision_on_page = (
                (lambda done, total: on_file_progress("vision", done, total))
                if on_file_progress is not None
                else None
            )
            _enrich_with_vision(parse_result, config, on_page=_vision_on_page)
        except VisionSystemicFailure as e:
            # Every Vision page failed AND those pages depended on Vision for
            # their content (scanned / garbled). Fail THIS file — don't write a
            # silently-empty knowledge base — but return cleanly so the rest of
            # the run keeps processing other files (the run loop has no
            # try/except around process_single_file, so re-raising would crash
            # the whole run). Mirrors the parse-failure early-return above.
            result.success = False
            result.error = str(e)
            result.error_type = "vision_systemic_failure"
            # Carry out the triage/failure tally even on this failure path so
            # run_log still shows what happened.
            vt = parse_result.metadata.get("vision_triage")
            if isinstance(vt, dict):
                result.vision_triage = {
                    k: int(v) for k, v in vt.items() if isinstance(v, int)
                }
            return result, []

    # --- Phase 1.6: Pre-write hooks (post-Vision, pre-frontmatter) ---
    # Hooks that enrich metadata without touching Vision (e.g. exiftool
    # metadata extraction). Runs after Vision so language detection and
    # Vision results are already settled before frontmatter is built.
    run_post_parse_hooks(file_path, parse_result, config, phase="pre_write")

    # --- Phase 1.7: Docling + Vision dedup (keep one half) ---
    # When Vision enrichment produced a whole-page view alongside Docling's,
    # output.vision_keep selects which half to keep (default: both = no-op).
    # Splits on the vision-enriched marker; auto-skips supplement-mode (xlsx)
    # and Mode B (docx) where the two halves are not a superset pair.
    parse_result.markdown = _apply_vision_keep(
        parse_result.markdown, config, parse_result.metadata.get("format")
    )

    # --- Phase 2: Write Markdown + assets ---
    # original_file determines the sources/*.md filename — must be the
    # user's input, not the Phase-0.5-converted xlsx in .cache/.
    # _WRITE_NAMES_GATE: `existing_names` is the cross-file name-collision
    # set; serialize the allocate-and-write so concurrent files can't pick
    # the same sources/*.md name.
    with _WRITE_NAMES_GATE:
        output_path = write_markdown(
            parse_result=parse_result,
            original_file=Path(result.original_file),
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
            f"Large document: {Path(result.original_file).name} → {md_size_mb:.1f}MB Markdown "
            f"(~{result.tokens_estimated:,} tokens). Chunking may be slow."
        )

    # --- Phase 3: Chunk (if enabled) ---
    chunks = []
    if chunker and get_nested(config, "chunking.enabled", True):
        t1 = time.monotonic()

        # Build document metadata for chunker.
        #
        # parse_result.metadata accumulates file-level fields throughout
        # parsing (Docling origin, file_metadata / aliases / tags hooks,
        # xlsx embedded image list, frontmatter timestamps, …). The
        # earlier ``**parse_result.metadata`` splat copied ALL of them
        # into every chunk's metadata, which then went straight into
        # chunks.jsonl. Real measurement on nra_kinou (152 chunks):
        # metadata accounted for 74% of the jsonl size and 19 of those
        # fields had byte-for-byte-identical values across every chunk —
        # textbook "file-level data, wrong scope."
        #
        # Solution: keep ONE source of truth for each field.
        #   - File-level fields → sources/*.md frontmatter + index.json.
        #     Chunks reference them via metadata.source / .original_file
        #     (path-based join). Do NOT copy them onto every chunk.
        #   - Chunk-level fields → stay on the chunk (chunk_index,
        #     total_chunks, tokens, title_path, sheet_name, has_table,
        #     has_image_ref, source, original_file, format, language).
        #     "source" and "original_file" are kept on the chunk because
        #     they're the join-key downstream uses; everything else
        #     file-level is dropped here.
        #
        # The blacklist is INTENTIONALLY hard-coded — these are facts
        # about which scope each field belongs to, not user preferences,
        # so a YAML knob would add complexity without value. If a
        # consumer ever needs binary_hash on chunks for dedup, the
        # ``lineage.original_input.binary_hash`` channel still carries
        # it (ARCHITECTURE.md §5.10).
        _CHUNK_METADATA_BLACKLIST = frozenset({
            # Pre-existing exclusion: 186 KB bbox dict, lives in
            # index.json (see _build_index_entry).
            "element_boxes",
            # Per-page PDF dimensions (points) — the sibling of element_boxes
            # (both are set together under output.include_bounding_boxes). It
            # is inherently per-file geometry, keyed by page number, with no
            # chunk-level consumer: the only readers are index_builder /
            # visualizer / cli, all of which look it up from index.json, never
            # from the chunk. On a 100-page doc it was ~40% of chunks.jsonl.
            "page_sizes",
            # Internal diagnostic flag set only when the OOM batch-fallback
            # parse path ran (docling_parser). It records HOW the parse was
            # done, not anything about this chunk — run history already lives
            # in log.md. Pure noise on chunks.
            "oom_batch_fallback_used",
            # Per-file Vision triage tally (sent_to_vision / triage_skipped /
            # ...) set once by _enrich_with_vision. It is whole-document run
            # stats, identical across every chunk of a file. Its real
            # consumers read it from FileResult.vision_triage -> run_log,
            # never from the chunk. Pure per-chunk duplication.
            "vision_triage",
            # File-level identity / Docling origin — promoted to top-level
            # frontmatter keys by the file_metadata hook; chunk gets the
            # same info via `lineage.original_input.{filename,mimetype,
            # binary_hash}`.
            "docling_origin",
            "docling_name",
            "mimetype",
            "binary_hash",
            # File-level embedded-asset registries. The full list lives in
            # index.json (per-file "files[].assets"); chunk-side image
            # references are conveyed via inline `<!-- image: ... -->`
            # markers in the chunk text itself. `embedded_images` is the
            # generic dict registry (docx + xlsx) driving full-res Vision;
            # `xlsx_embedded_images` is the older path-only list.
            "xlsx_embedded_images",
            "embedded_images",
            # File-level warnings (e.g. "Excel produced 19 pages, only
            # first 10 sent to Vision") — surfaced to sources/*.md
            # frontmatter, not per-chunk relevant.
            "warnings",
            # File-level timestamps / search-tags. The frontmatter writer
            # already emits them per file; chunks duplicate without gain.
            # NOTE: last_modified is deliberately NOT here — it is also
            # added unconditionally a few lines below from file_path.stat()
            # (kept on chunks because some retrieval pipelines use it as
            # an incremental-indexing watermark, where having it per-chunk
            # makes sense even though the value is constant per file).
            "created",
            "created_source",
            "aliases",
            "tags",
            # File-level scalar/booleans that describe the whole document,
            # not this chunk. The chunk has its own narrower analogues
            # `has_table` (singular) and `has_image_ref` (set by
            # _postprocess_chunks based on chunk.text).
            "pages",
            "has_images",
            "has_tables",
            "suffix_format",        # only set when magika overrode the suffix
            "hidden_text",          # PDF-only doc-level flag
            "structured_extractions_per_page",  # hook-internal, pre-Vision
            # xlsx-only file-level fields produced by docling_parser and
            # utils.xlsx_sheet_map. They drive pipeline routing decisions
            # (batched Vision trigger, connector hook page anchor, sheet→
            # page mapping for any future sheet-aware code) and must STAY
            # on parse_result.metadata so those consumers can read them.
            # On chunks they're pure duplication — the sheet a chunk
            # belongs to is already in metadata.sheet_name + .title_path;
            # chunks don't need the whole-workbook map. Measured cost on
            # a 13-sheet xlsx: ~295 KB of jsonl bloat (~17% of file size)
            # for zero retrieval value.
            "xlsx_sheet_page_map",
            "xlsx_visible_sheet_names",
            "xlsx_visible_sheet_count",
            # Word counterpart of the xlsx map above: full PDF text layer
            # keyed by page number (~100 KB on a 92-page thesis), consumed
            # only by the per-page Vision ground-truth slicing during the
            # run. Zero retrieval value per chunk.
            "docx_page_texts",
        })
        parse_meta_for_chunks = {
            k: v for k, v in parse_result.metadata.items()
            if k not in _CHUNK_METADATA_BLACKLIST
        }
        # original_file and last_modified MUST reflect the user's input,
        # not the Phase-0.5 converted .xlsx in .cache/ (which has a stem
        # like "<sha256>" and an mtime equal to the conversion moment).
        original_path = Path(result.original_file)
        doc_metadata = {
            "source": result.output_path,
            "original_file": str(original_path.name),
            "format": result.format,
            **parse_meta_for_chunks,
        }

        # Enrich metadata: language detection (if not already set)
        if "language" not in doc_metadata:
            doc_metadata["language"] = _detect_language(parse_result.markdown)

        # Enrich metadata: file modification time
        try:
            mtime = original_path.stat().st_mtime
            import datetime
            doc_metadata["last_modified"] = datetime.datetime.fromtimestamp(
                mtime
            ).isoformat(timespec="seconds")
        except Exception:
            pass

        # Dedup already applied in Phase 1.7 (before write_markdown).
        # parse_result.markdown is already deduplicated — use directly.
        chunk_markdown = parse_result.markdown

        try:
            chunks = chunker.chunk(chunk_markdown, doc_metadata)
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

        # Attach per-chunk lineage — the provenance trail of what parser /
        # hooks / vision / chunker produced this chunk. Built once and
        # applied to every chunk (they share the same file-level trail;
        # chunk-level differences live in existing flat metadata like
        # title_path / chunk_index). Copy on write so post-processing
        # anywhere else can't mutate one chunk's lineage into another's.
        if chunks:
            # original_file MUST be the user's input path (not the working
            # path), so that for Phase-0.5-converted .xls files the lineage
            # records "画面遷移図.xls" rather than the internal cached
            # ".cache/_xls_convert/<sha>.xlsx".
            lineage = _build_chunk_lineage(
                parse_result=parse_result,
                chunker=chunker,
                original_file=Path(result.original_file),
                source_md_rel=result.output_path,
            )
            for chunk in chunks:
                # Deep-ish copy: transformations list is freshly built per
                # file, but cloning once more here gives each chunk its
                # own dict identity so downstream consumers can mutate
                # freely without cross-chunk side effects.
                chunk.metadata["lineage"] = {
                    "source_markdown": lineage["source_markdown"],
                    "original_input": dict(lineage["original_input"]),
                    "transformations": [dict(t) for t in lineage["transformations"]],
                }

        result.chunks_count = len(chunks)
        result.chunk_time_ms = int((time.monotonic() - t1) * 1000)

    # Carry per-file bounding boxes through to index_builder. Lives on
    # FileResult (not in the .md frontmatter which only holds scalars)
    # so index.json can expose them per-file without re-parsing Docling.
    eb = parse_result.metadata.get("element_boxes")
    if eb:
        result.element_boxes = eb
    # Same path for the per-page dimensions that pair with element_boxes.
    ps = parse_result.metadata.get("page_sizes")
    if ps:
        result.page_sizes = ps

    # Collect non-fatal warnings produced during this file's processing
    # (page caps, OCR fallbacks, etc.) for run_pipeline to aggregate.
    # parse_result.metadata["warnings"] is a list[str] written by phases
    # like _generate_page_images_via_libreoffice.
    md_warnings = parse_result.metadata.get("warnings")
    if isinstance(md_warnings, list):
        result.warnings = [str(w) for w in md_warnings if w]

    # Carry the per-file Vision triage tally out for run_pipeline to sum.
    vt = parse_result.metadata.get("vision_triage")
    if isinstance(vt, dict):
        result.vision_triage = {k: int(v) for k, v in vt.items() if isinstance(v, int)}

    return result, chunks


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _collect_asset_rels_for_file(
    file_path: Path,
    output_dir: Path,
    config: dict[str, Any],
) -> list[str]:
    """
    Collect asset files belonging to a given source file.

    Assets are named with the source file's stem as prefix:
      - {stem}-page-NNN.png   (LibreOffice page screenshots)
      - {stem}-image*.png     (xlsx embedded images)
      - {stem}-p{N}-chart.png (PDF chart extractions)

    Returns relative paths from output_dir (forward-slash normalized).
    """
    assets_dir_name = get_nested(config, "output.assets_dir", "assets")
    assets_dir = output_dir / assets_dir_name
    if not assets_dir.exists():
        return []

    stem = file_path.stem
    rels: list[str] = []
    for asset in assets_dir.iterdir():
        if asset.is_file() and asset.name.startswith(f"{stem}-"):
            rel = f"{assets_dir_name}/{asset.name}"
            rels.append(rel)
    return sorted(rels)


def _finalize_artifacts(output_dir: Path, config: dict[str, Any]) -> None:
    """
    Post-run cleanup of runtime-dependency artefacts the caller did NOT ask
    to keep.

    Some artefacts (assets/, index.json) cannot be skipped at generation
    time because the pipeline itself consumes them mid-run — assets feed
    Vision, index.json is read by knowledge_map and built incrementally. So
    the facade's outputs whitelist marks them for *cleanup*: produce as
    usual, delete here once everything that needed them has run.

    The cleanup set is passed via ``output._cleanup`` (a list of tokens set
    by api._apply_output_whitelist). Tokens: "index" / "assets" / "errors".
    Absent / empty → this function is a no-op (legacy behaviour, full output).

    Invariants:
      * ``.cache/`` is NEVER touched — deleting it would break incremental.
      * Deleting assets/ also clears ``outputs.assets`` in every meta.json so
        the next incremental run's is_cache_valid() asset-existence check
        passes (otherwise every cached file would invalidate → full re-run,
        re-burning Vision cost on repeated markdown-only runs).
      * Best-effort: a cleanup failure logs a warning, never raises — a
        successful ingest must not be turned into a failure by tidy-up.
    """
    cleanup = get_nested(config, "output._cleanup", None)
    if not cleanup:
        return
    cleanup_set = set(cleanup)

    # --- assets/ : delete dir + clear meta references ---
    if "assets" in cleanup_set:
        assets_dir = output_dir / get_nested(config, "output.assets_dir", "assets")
        if assets_dir.exists():
            try:
                shutil.rmtree(assets_dir)
            except OSError as e:
                _pipeline_logger.warning(f"Could not remove assets dir: {e}")
        # Keep meta.json consistent with the now-empty assets so incremental
        # cache stays valid on the next run (see is_cache_valid asset check).
        cache_dir = output_dir / get_nested(config, "incremental.cache_dir", ".cache")
        if cache_dir.exists():
            for meta_path in cache_dir.glob("*.meta.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                outputs = meta.get("outputs")
                if isinstance(outputs, dict) and outputs.get("assets"):
                    outputs["assets"] = []
                    try:
                        meta_path.write_text(
                            json.dumps(meta, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    except OSError as e:
                        _pipeline_logger.warning(
                            f"Could not update meta {meta_path.name} after "
                            f"assets cleanup: {e}"
                        )

    # --- index.json : safe to delete (cache stores index_entry in meta) ---
    if "index" in cleanup_set:
        index_file = output_dir / get_nested(config, "output.index_file", "index.json")
        if index_file.exists():
            try:
                index_file.unlink()
            except OSError as e:
                _pipeline_logger.warning(f"Could not remove index.json: {e}")

    # --- chunks.jsonl : built only because a dependency (knowledge_map)
    # needed it, but the user didn't ask to keep it → delete + clear the
    # cached chunk_ids so the next incremental run's is_cache_valid()
    # chunk-presence check passes (same consistency rule as assets above). ---
    if "chunks" in cleanup_set:
        chunks_file = output_dir / get_nested(config, "chunking.output_file", "chunks.jsonl")
        if chunks_file.exists():
            try:
                chunks_file.unlink()
            except OSError as e:
                _pipeline_logger.warning(f"Could not remove chunks.jsonl: {e}")
        cache_dir = output_dir / get_nested(config, "incremental.cache_dir", ".cache")
        if cache_dir.exists():
            for meta_path in cache_dir.glob("*.meta.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                outputs = meta.get("outputs")
                if isinstance(outputs, dict) and outputs.get("chunk_ids"):
                    outputs["chunk_ids"] = []
                    try:
                        meta_path.write_text(
                            json.dumps(meta, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    except OSError as e:
                        _pipeline_logger.warning(
                            f"Could not update meta {meta_path.name} after "
                            f"chunks cleanup: {e}"
                        )

    # --- errors.json : companion of structural artefacts; drop on minimal runs ---
    if "errors" in cleanup_set:
        report_file = get_nested(config, "error_handling.report_file", "errors.json")
        errors_path = output_dir / report_file
        if errors_path.exists():
            try:
                errors_path.unlink()
            except OSError as e:
                _pipeline_logger.warning(f"Could not remove errors.json: {e}")


def _parse_frontmatter(markdown: str) -> dict[str, Any]:
    """
    Extract YAML frontmatter from a Markdown string written by our pipeline.

    Returns parsed dict, or empty dict if no valid frontmatter found.
    """
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(markdown[4:end])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _make_index_parse_result(
    file_result: FileResult,
    output_dir: Path,
) -> "ParseResult":
    """
    Create a lightweight ParseResult from FileResult for IndexBuilder.

    Reads the written .md file (just produced in Phase 2) and extracts
    metadata from its YAML frontmatter so fields like language, pages,
    has_tables etc. flow into index.json without extra plumbing.
    """
    from .parsers.base import ParseResult

    md_path = output_dir / file_result.output_path
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except Exception:
        markdown = ""

    # Start with minimal fallback, then overlay frontmatter fields
    metadata: dict[str, Any] = {
        "format": file_result.format,
        "title": Path(file_result.original_file).stem,
    }
    metadata.update(_parse_frontmatter(markdown))

    # element_boxes is not a scalar and was never written to the .md
    # frontmatter, so pipe it through FileResult directly. IndexBuilder
    # picks it up and writes a per-file entry into index.json.
    if file_result.element_boxes:
        metadata["element_boxes"] = file_result.element_boxes
    # Same for page_sizes — index_builder writes it per-file alongside
    # element_boxes so the visualizer can scale by true page size.
    if file_result.page_sizes:
        metadata["page_sizes"] = file_result.page_sizes

    return ParseResult(
        markdown=markdown,
        metadata=metadata,
    )


def run_pipeline(
    input_paths: list[Path | str],
    config: dict[str, Any],
    parser: BaseParser,
    chunker: BaseChunker | None = None,
    *,
    acknowledge_large: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    install_signal_handler: bool = False,
) -> PipelineResult:
    """
    Run the full DocIngest pipeline.

    Args:
        input_paths: Files or directories to process.
        config: Merged configuration dict.
        parser: Parser instance.
        chunker: Chunker instance (None if chunking disabled).
        acknowledge_large: When safety.mode == "strict" and the pre-run
            budget check flags violations, set True to proceed anyway.
            Ignored when safety is off or in warn mode. Callers (CLI's
            --yes, MCP's acknowledge_large argument, or Python API users)
            expose this flag; the default False preserves strict mode's
            refuse-to-run behaviour.
        on_progress: Optional callback invoked once per file completion
            (cached, processed, failed, or skipped due to interrupt).
            Receives a single dict argument — see "Progress events" below.
            Exceptions raised from the callback are swallowed (logged at
            warning level) so a buggy callback cannot break the run.
        install_signal_handler: When True, install a SIGINT handler for
            the duration of the run so Ctrl+C triggers a graceful stop
            (finish current file, write aggregates, exit). Default False
            because library callers usually want their own signal handling
            to stay in effect; the CLI passes True. No-op when not on the
            main thread.

    Progress events:
        Each call to ``on_progress`` receives a dict shaped like::

            {
                "kind":        "file_done",
                "status":      "cached" | "added" | "updated" | "forced"
                               | "failed" | "skipped",
                "file":        "<basename>",
                "current":     <1-based count of completed files>,
                "total":       <total files in this run>,
                "chunks":      <chunks produced for this file>,
                "elapsed_ms":  <int — 0 for cached / skipped>,
                "error":       <str | None — populated when status=="failed">,
                "error_type":  <str — "" when success>,
            }

        A second, OPTIONAL event kind reports progress WITHIN a single file so
        a UI isn't frozen during a long file (Vision is the worst offender —
        measured 76 s of silence on a 1.9 MB PDF). Consumers that only handle
        ``kind == "file_done"`` ignore it automatically (backwards-compatible)::

            {
                "kind":        "file_progress",
                "phase":       "vision",           # the sub-stage reporting
                "file":        "<basename>",
                "current":     <1-based index of the file being processed>,
                "total":       <total files in this run>,
                "sub_current": <Vision pages completed so far>,
                "sub_total":   <Vision pages sent (post-triage/cap)>,
            }

        Cadence of file_progress is ``parsing.vision.progress_interval``
        (default 1 = every page; raise it for very long documents).

    Returns:
        PipelineResult with details of all processed files. When safety
        strict mode aborts the run, result.safety.aborted is True and
        result.total_files reflects the discovered count while
        result.successful and result.files stay empty.
    """
    t_start = time.monotonic()

    # A stop requested for a PREVIOUS run must not kill this one.
    _EXTERNAL_STOP.clear()

    # Resolve output directory
    output_dir = Path(get_nested(config, "output.dir", "./knowledge"))
    output_dir.mkdir(parents=True, exist_ok=True)

    # Discover files (with zip expansion when enabled)
    files, invalid_inputs = discover_files(input_paths, config=config)
    pipeline_result = PipelineResult(total_files=len(files) + len(invalid_inputs))

    # Surface invalid inputs as standard io_error entries — this is what
    # turns the silent "successful=0 with no errors" cross-container failure
    # mode into a visible result.failed=N + result.stats["errors"]
    # populated outcome. Each invalid path becomes one FileResult so it
    # shows up in result.files[] and run_log alongside real processing
    # failures.
    for inv in invalid_inputs:
        fr = FileResult(
            original_file=inv.input,
            success=False,
            error=f"{inv.reason}: {inv.detail}" if inv.detail else inv.reason,
            error_type="io_error",
            status="failed",
        )
        pipeline_result.files.append(fr)
        pipeline_result.failed += 1
        pipeline_result.errors.append({
            "file": inv.input,
            "error": fr.error,
            "error_type": "io_error",
            "reason": inv.reason,
        })

    # (total_files already covers both valid + invalid above; PipelineResult
    # was constructed with that count so a batch of [valid.pdf, missing.pdf]
    # reports total_files=2, failed=1, successful=1 — not the silent
    # total_files=1 that hid the bug before.)

    if not files:
        # No valid files to process. Still write a brief run log entry below
        # by falling through normally — but skip the heavy phases. If there
        # were invalid_inputs we've already populated errors, so callers
        # see a clear failed > 0 signal.
        pipeline_result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return pipeline_result

    # --- Phase 0: Safety budget check ---
    # Runs BEFORE any parser / LLM call so over-budget files are surfaced
    # before cost is incurred. Three modes:
    #   off    — skipped entirely (legacy behaviour for this codepath).
    #   warn   — log violations, continue.
    #   strict — refuse unless acknowledge_large=True.
    # Any failure inside the safety module is swallowed (see safety.py's
    # try/except wrappers) so a Phase 0 bug can never block the pipeline.
    safety_mode = str(get_nested(config, "safety.mode", "strict") or "strict").lower()
    safety_enabled = get_nested(config, "safety.enabled", True)
    if safety_enabled and safety_mode != "off":
        try:
            from .safety import check_budget, format_violations
            violations, summary = check_budget(files, config)
            # Keep the exposed safety dict light — don't pack per-file
            # `infos` (which can be large) into PipelineResult. Callers
            # that need per-file details can call inspect_files directly.
            pipeline_result.safety = {
                "mode": safety_mode,
                "violations": violations,
                "summary": {
                    "total_files": summary["total_files"],
                    "total_pages": summary["total_pages"],
                    "total_est_cost_usd": summary["total_est_cost_usd"],
                },
                "acknowledged": bool(acknowledge_large),
                "aborted": False,
            }
            if violations:
                rendered = format_violations(violations)
                if safety_mode == "strict" and not acknowledge_large:
                    pipeline_result.safety["aborted"] = True
                    _pipeline_logger.warning(
                        f"Safety check (strict): run aborted — "
                        f"{len(violations)} violation(s).\n{rendered}\n"
                        f"Pass acknowledge_large=True (Python / MCP) or "
                        f"--yes (CLI) to proceed."
                    )
                    pipeline_result.elapsed_ms = int(
                        (time.monotonic() - t_start) * 1000
                    )
                    return pipeline_result
                # warn, or strict + acknowledged → log and continue.
                _pipeline_logger.warning(
                    f"Safety check: {len(violations)} violation(s), mode="
                    f"{safety_mode}"
                    f"{' (acknowledged)' if acknowledge_large else ''} "
                    f"— proceeding.\n{rendered}"
                )
        except Exception as e:
            # Phase 0 is advisory; a bug here must not kill the pipeline.
            _pipeline_logger.warning(f"Safety check failed, skipping: {e}")

    # Progress reporting — opt-in callback fired once per file completion.
    # `_progress_total` is fixed at the top of the run; `_progress_done`
    # increments uniformly across cached / processed / skipped paths so
    # `current/total` is meaningful for a UI progress bar.
    _progress_total = len(files)
    _progress_done = 0

    def _emit_progress(
        *,
        status: str,
        file_basename: str,
        chunks: int = 0,
        elapsed_ms: int = 0,
        error: str | None = None,
        error_type: str = "",
    ) -> None:
        nonlocal _progress_done
        _progress_done += 1
        if on_progress is None:
            return
        event = {
            "kind": "file_done",
            "status": status,
            "file": file_basename,
            "current": _progress_done,
            "total": _progress_total,
            "chunks": chunks,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "error_type": error_type,
        }
        try:
            on_progress(event)
        except Exception as e:
            _pipeline_logger.warning(
                f"on_progress callback raised "
                f"{type(e).__name__}: {e}; ignored."
            )

    # Track used output filenames (for dedup across files)
    existing_names: set[str] = set()

    # Index builder for index.json
    index_builder = IndexBuilder(config)

    # Collect all chunks for writing to chunks.jsonl.
    # Note: final chunks.jsonl = reused_chunk_records (from cache) + new_chunks (from processing)
    new_chunks: list = []
    reused_chunk_records: list[dict[str, Any]] = []

    # --- Incremental mode setup ---
    incremental_enabled = get_nested(config, "incremental.enabled", True)
    force_rebuild = get_nested(config, "incremental.force", False)

    cache_dir = output_dir / get_nested(config, "incremental.cache_dir", ".cache")
    config_hash = compute_config_hash(config) if incremental_enabled else ""

    # Load old chunks.jsonl once for cache validation + reuse
    old_chunks_file = output_dir / get_nested(config, "chunking.output_file", "chunks.jsonl")
    old_chunks_by_id: dict[str, dict[str, Any]] = {}
    if incremental_enabled and not force_rebuild:
        old_chunks_by_id = load_chunks_by_id(old_chunks_file)
        _pipeline_logger.info(
            f"Incremental: loaded {len(old_chunks_by_id)} existing chunks from {old_chunks_file.name}"
        )

    # Partition files: cached vs. to_process.
    # to_process carries the prior meta (or None) and a cache-miss reason so
    # run_log can distinguish "added" (no prior meta) from "updated" (meta
    # existed but invalidated) and surface WHY the invalidation happened.
    cached_files: list[tuple[Path, dict[str, Any], str]] = []  # (path, meta, cache_key)
    to_process: list[tuple[Path, dict[str, Any] | None, str]] = []  # (path, prior_meta, reason)

    if incremental_enabled and not force_rebuild:
        for file_path in files:
            try:
                cache_key = compute_cache_key(file_path)
            except (FileNotFoundError, PermissionError, OSError) as e:
                _pipeline_logger.warning(f"Cannot hash {file_path.name}: {e}")
                to_process.append((file_path, None, "hash failed"))
                continue

            meta = load_cached_meta(cache_dir, cache_key)
            if meta is None:
                to_process.append((file_path, None, ""))
                continue

            valid, reason = is_cache_valid(meta, config_hash, output_dir, old_chunks_by_id)
            if valid:
                cached_files.append((file_path, meta, cache_key))
            else:
                _pipeline_logger.debug(
                    f"Cache miss for {file_path.name}: {reason}"
                )
                to_process.append((file_path, meta, reason))

        _pipeline_logger.info(
            f"Incremental: {len(cached_files)} cached, {len(to_process)} to process "
            f"(out of {len(files)} total)"
        )
    else:
        # Full-rebuild paths (incremental disabled, or --force). prior_meta
        # stays None — run_log uses force_rebuild directly to tag "forced".
        to_process = [(f, None, "") for f in files]
        if force_rebuild:
            _pipeline_logger.info("Incremental: --force flag set, full rebuild")

    # --- Process cached files: reuse outputs, no Phase 1-3 work ---
    for file_path, meta, cache_key in cached_files:
        # Populate FileResult for reporting
        file_result = FileResult(
            original_file=str(file_path),
            output_path=meta["outputs"]["source_md"],
            format=meta.get("format", "unknown"),
            success=True,
            chunks_count=len(meta["outputs"].get("chunk_ids", [])),
            tokens_estimated=meta.get("index_entry", {}).get("tokens_estimated", 0),
            parse_time_ms=0,
            chunk_time_ms=0,
            status="cached",
        )
        pipeline_result.files.append(file_result)
        pipeline_result.successful += 1
        pipeline_result.total_chunks += file_result.chunks_count
        pipeline_result.total_tokens += file_result.tokens_estimated

        # Reuse index entry
        index_builder.add_cached_entry(meta["index_entry"])

        # Reuse chunks (lookup by id in old chunks.jsonl)
        for chunk_id in meta["outputs"].get("chunk_ids", []):
            record = old_chunks_by_id.get(chunk_id)
            if record is not None:
                reused_chunk_records.append(record)

        # Reserve output filename so new files don't collide with cached ones
        source_md_rel = meta["outputs"].get("source_md", "")
        if source_md_rel:
            existing_names.add(Path(source_md_rel).name)

        # Update last_seen_path if it changed (file moved/renamed)
        new_path_str = str(file_path.resolve()).replace("\\", "/")
        if meta.get("last_seen_path") != new_path_str:
            meta["last_seen_path"] = new_path_str
            try:
                save_cached_meta(cache_dir, meta)
            except OSError as e:
                _pipeline_logger.warning(f"Could not update meta for {file_path.name}: {e}")

        _emit_progress(
            status="cached",
            file_basename=file_path.name,
            chunks=file_result.chunks_count,
        )

    # --- Process uncached files: full pipeline ---
    # Graceful interrupt: first Ctrl+C sets a flag; the loop finishes the
    # current file then breaks out to write aggregate outputs (chunks.jsonl,
    # index.json, knowledge_map). Second Ctrl+C re-raises KeyboardInterrupt
    # for a hard exit. signal.signal() must run on the main thread; if we're
    # not on it (e.g. embedded in another runtime) we skip silently and the
    # default SIGINT behaviour stands.
    import signal as _signal
    import threading as _threading

    _stop_requested = {"flag": False}
    _prev_sigint = None
    _installed_handler = False

    def _on_sigint(_signum, _frame):
        if _stop_requested["flag"]:
            # Second interrupt → restore default and re-raise so the user
            # gets an immediate exit.
            _signal.signal(_signal.SIGINT, _signal.default_int_handler)
            raise KeyboardInterrupt
        _stop_requested["flag"] = True
        _pipeline_logger.warning(
            "Interrupt received — finishing current file then writing "
            "aggregate outputs. Press Ctrl+C again to force exit."
        )

    # Install only when explicitly requested AND on the main thread.
    # CLI passes install_signal_handler=True; library callers default to
    # False so DocIngest does not preempt the host's own signal handling.
    if install_signal_handler and _threading.current_thread() is _threading.main_thread():
        try:
            _prev_sigint = _signal.signal(_signal.SIGINT, _on_sigint)
            _installed_handler = True
        except (ValueError, OSError):
            # ValueError: not in main thread; OSError: signal unavailable.
            # Either way, fall through with no handler.
            _installed_handler = False

    # --- File-level concurrency setup ---
    # file_concurrency == 1 (default): the sequential path below, behaviour
    # identical to the pre-concurrency pipeline. > 1: files overlap — file B
    # parses (exclusively, behind _PARSE_GATE) while file A waits on Vision
    # I/O. Parsing itself NEVER runs concurrently; see the gate comments at
    # module top for the probe-verified reasons.
    file_concurrency = max(1, int(get_nested(config, "performance.file_concurrency", 1)))
    global _VISION_GATE
    _vg_raw = get_nested(config, "performance.vision_global_concurrency", None)
    _vg = (
        int(_vg_raw) if _vg_raw is not None
        else int(get_nested(config, "performance.parallel_files", 4))
    )
    _VISION_GATE = threading.BoundedSemaphore(max(1, _vg))

    def _make_file_progress(name: str):
        """Within-file progress (Vision pages) → emit a distinct
        kind="file_progress" event. Old consumers that filter on
        kind=="file_done" ignore it automatically (backwards-compatible).
        current/total are file-level (_progress_done+1 of _progress_total —
        under concurrency this is the completed-count, an approximation);
        sub_current/sub_total are the within-file Vision page counts.
        No-op unless the caller passed on_progress."""
        def _on_file_progress(phase: str, sub_done: int, sub_total: int) -> None:
            if on_progress is None:
                return
            try:
                on_progress({
                    "kind": "file_progress",
                    "phase": phase,
                    "file": name,
                    "current": _progress_done + 1,
                    "total": _progress_total,
                    "sub_current": sub_done,
                    "sub_total": sub_total,
                })
            except Exception as e:
                _pipeline_logger.warning(
                    f"on_progress (file_progress) raised "
                    f"{type(e).__name__}: {e}; ignored."
                )
        return _on_file_progress

    def _aggregate_one(
        file_path: Path,
        prior_meta,
        cache_reason: str,
        file_result,
        file_chunks: list,
    ) -> None:
        """Order-sensitive bookkeeping for one processed file. ALWAYS runs on
        the main thread in to_process order — regardless of file_concurrency,
        index.json file order, chunks.jsonl order and the progress counter
        match a sequential run exactly."""
        # Tag lifecycle status for run_log. Order matters:
        #   failure wins → forced wins over added/updated (it's a full
        #   rebuild regardless of prior state) → prior_meta distinguishes
        #   "added" (first time) from "updated" (cache invalidated).
        if not file_result.success:
            file_result.status = "failed"
        elif force_rebuild:
            file_result.status = "forced"
        elif prior_meta is None:
            file_result.status = "added"
        else:
            file_result.status = "updated"
            file_result.cache_reason = cache_reason

        pipeline_result.files.append(file_result)

        # Aggregate per-file warnings into the run-level summary.
        # Done independent of success — a partially-processed file may
        # still have surfaced quality warnings worth visible at the
        # aggregate level (e.g. a docx that hit page cap but didn't fail).
        for w in file_result.warnings:
            pipeline_result.warnings.append({
                "file": file_path.name,
                "message": w,
            })

        # Sum the per-file Vision triage tally into the corpus-wide total.
        # Cached files carry an empty dict (they didn't re-run Vision), so
        # this reflects work actually done THIS run — not a stale all-time
        # total. Same key set across files; missing keys default to 0.
        for k, v in (file_result.vision_triage or {}).items():
            pipeline_result.vision_triage[k] = (
                pipeline_result.vision_triage.get(k, 0) + int(v)
            )

        if file_result.success:
            pipeline_result.successful += 1
            pipeline_result.total_chunks += file_result.chunks_count
            pipeline_result.total_tokens += file_result.tokens_estimated
            new_chunks.extend(file_chunks)

            # Add to index (returns the entry so we can store it in meta.json)
            index_entry = index_builder.add_file(
                parse_result=_make_index_parse_result(file_result, output_dir),
                original_file=file_path,
                output_path=output_dir / file_result.output_path,
                output_dir=output_dir,
                chunks_count=file_result.chunks_count,
            )

            # Persist meta.json for next incremental run
            if incremental_enabled:
                try:
                    from .output.chunks_writer import build_chunk_id
                    cache_key = compute_cache_key(file_path)
                    chunk_ids = [build_chunk_id(c) for c in file_chunks]
                    asset_rels = _collect_asset_rels_for_file(file_path, output_dir, config)
                    meta = build_meta(
                        file_path=file_path,
                        cache_key=cache_key,
                        config_hash=config_hash,
                        format_str=file_result.format,
                        source_md_rel=file_result.output_path,
                        asset_rels=asset_rels,
                        chunk_ids=chunk_ids,
                        index_entry=index_entry,
                    )
                    save_cached_meta(cache_dir, meta)
                except Exception as e:
                    _pipeline_logger.warning(
                        f"Could not save incremental cache for {file_path.name}: {e}"
                    )
        else:
            pipeline_result.failed += 1
            pipeline_result.errors.append({
                "file": file_result.original_file,
                "error": file_result.error,
                "error_type": file_result.error_type,
            })
            index_builder.add_error()

        # Emit one progress event per processed file. file_result.status
        # is one of: added | updated | forced | failed (set above).
        _emit_progress(
            status=file_result.status or "added",
            file_basename=file_path.name,
            chunks=file_result.chunks_count,
            elapsed_ms=file_result.parse_time_ms + file_result.chunk_time_ms,
            error=file_result.error or None,
            error_type=file_result.error_type,
        )

    try:
        if file_concurrency == 1:
            # Sequential path — the pre-concurrency behaviour, literally.
            for _idx, (file_path, prior_meta, cache_reason) in enumerate(to_process):
                # Two stop sources, same semantics: SIGINT (CLI) or
                # request_stop() (GUI / library host on a worker thread).
                if _stop_requested["flag"] or _EXTERNAL_STOP.is_set():
                    remaining_paths = [t[0] for t in to_process[_idx:]]
                    _pipeline_logger.warning(
                        f"Stopping early due to interrupt; "
                        f"{len(remaining_paths)} file(s) left unprocessed. "
                        f"Aggregate outputs will be written for completed files."
                    )
                    pipeline_result.interrupted = True
                    # Emit one "skipped" event per remaining file so a UI's
                    # progress bar can still reach 100% — the run is over,
                    # the caller should know how many were dropped.
                    for skipped in remaining_paths:
                        _emit_progress(
                            status="skipped",
                            file_basename=skipped.name,
                        )
                    break

                file_result, file_chunks = process_single_file(
                    file_path=file_path,
                    parser=parser,
                    chunker=chunker,
                    config=config,
                    output_dir=output_dir,
                    existing_names=existing_names,
                    on_file_progress=_make_file_progress(file_path.name),
                )
                _aggregate_one(
                    file_path, prior_meta, cache_reason, file_result, file_chunks
                )
        else:
            # Pipeline-overlap path: workers run process_single_file (parse
            # serialized by _PARSE_GATE, Vision capped by _VISION_GATE,
            # markdown writes serialized by _WRITE_NAMES_GATE); the main
            # thread consumes results in SUBMISSION order so every aggregate
            # output is ordered exactly like a sequential run.
            from concurrent.futures import ThreadPoolExecutor

            def _work_one(fp: Path):
                """Heavy per-file work. None = skipped by a stop request
                before it started (same degrade as the sequential skip)."""
                if _stop_requested["flag"] or _EXTERNAL_STOP.is_set():
                    return None
                return process_single_file(
                    file_path=fp,
                    parser=parser,
                    chunker=chunker,
                    config=config,
                    output_dir=output_dir,
                    existing_names=existing_names,
                    on_file_progress=_make_file_progress(fp.name),
                )

            with ThreadPoolExecutor(
                max_workers=file_concurrency,
                thread_name_prefix="docingest-file",
            ) as _file_pool:
                _futures = [
                    _file_pool.submit(_work_one, fp) for fp, _, _ in to_process
                ]
                for (file_path, prior_meta, cache_reason), _fut in zip(
                    to_process, _futures
                ):
                    try:
                        outcome = _fut.result()
                    except BaseException:
                        # Abort like the sequential path would: queued workers
                        # see the flag at start and bail; then propagate.
                        _stop_requested["flag"] = True
                        raise
                    if outcome is None:
                        if not pipeline_result.interrupted:
                            pipeline_result.interrupted = True
                            _pipeline_logger.warning(
                                "Stopping early due to interrupt; remaining "
                                "files are skipped. Aggregate outputs will be "
                                "written for completed files."
                            )
                        _emit_progress(
                            status="skipped",
                            file_basename=file_path.name,
                        )
                        continue
                    _aggregate_one(file_path, prior_meta, cache_reason, *outcome)
    finally:
        if _installed_handler:
            try:
                _signal.signal(_signal.SIGINT, _prev_sigint)
            except (ValueError, OSError):
                pass

    # Write index.json
    index_builder.write_index(output_dir)

    # Write chunks.jsonl: merge reused (from cache) + new chunks
    if get_nested(config, "chunking.enabled", True):
        from .output.chunks_writer import build_chunk_id, write_chunk_records
        new_records = [
            {
                "id": build_chunk_id(c),
                "text": c.text,
                "metadata": c.metadata,
            }
            for c in new_chunks
        ]
        all_final_records = reused_chunk_records + new_records
        if all_final_records:
            write_chunk_records(all_final_records, output_dir, config)

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

        # Stage 2 of tags derivation: append kw/<word> tags to each
        # sources/*.md frontmatter based on the corpus-wide TF-IDF
        # filtered keyword index. Stage 1 (format/lang) ran during the
        # pre_write hook for each file. Loaded lazily so a knowledge_map
        # generation failure doesn't take this down with it.
        try:
            from .output.tags_enrichment import enrich_sources_with_tags
            km_path = output_dir / get_nested(
                config, "knowledge_map.output_file", "knowledge_map.yaml"
            )
            if km_path.exists():
                km_data = yaml.safe_load(km_path.read_text(encoding="utf-8"))
                if isinstance(km_data, dict):
                    enrich_sources_with_tags(km_data, output_dir, config)
        except Exception as e:
            _pipeline_logger.warning(f"Tags enrichment failed: {e}")

    # Write errors.json if any failures, OR remove a stale one from a prior
    # failing run when this run succeeded. errors.json is a per-run snapshot
    # (run_log.py docstring); leaving last run's failures behind makes a
    # green run look broken.
    report_file = get_nested(config, "error_handling.report_file", "errors.json")
    errors_path = output_dir / report_file
    if pipeline_result.errors:
        errors_path.write_text(
            json.dumps(pipeline_result.errors, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    elif errors_path.exists():
        try:
            errors_path.unlink()
        except OSError as e:
            _pipeline_logger.warning(f"Could not remove stale errors.json: {e}")

    # Quality report: scan sources/*.md for [?] and [unreadable] markers
    # left by Vision when it encountered partially-readable content. Gives
    # a quick health check on output accuracy.
    if get_nested(config, "quality_report.enabled", True):
        try:
            from .output.quality_report import generate_report
            sources_dir = output_dir / get_nested(
                config, "output.sources_dir", "sources"
            )
            report_filename = get_nested(
                config, "quality_report.output_file", "quality_report.json"
            )
            pipeline_result.quality = generate_report(
                sources_dir=sources_dir,
                output_path=output_dir / report_filename,
            )
        except Exception as e:
            _pipeline_logger.warning(f"Quality report generation failed: {e}")

    pipeline_result.elapsed_ms = int((time.monotonic() - t_start) * 1000)

    # Collect LLM API token usage BEFORE the run-log write so the header's
    # LLM-cost tail (e.g. "LLM: 14 calls / 48,230 tok") reflects this run.
    # Earlier ordering wrote the log first and the tail was always empty.
    from .models.token_tracker import token_tracker
    pipeline_result.token_usage = token_tracker.summary()
    token_tracker.reset()

    # Run log: append-only timeline across runs. Independent of errors.json
    # and quality_report.json (single-run snapshots overwritten each run) —
    # log.md accumulates one section per run so users can see what changed
    # and when. Writes after elapsed_ms is set so the timeline reflects the
    # final state. Never raises — a log write failure leaves the pipeline
    # result intact.
    if get_nested(config, "run_log.enabled", True):
        try:
            from .output.run_log import append_run_entry
            append_run_entry(
                pipeline_result=pipeline_result,
                output_dir=output_dir,
                log_filename=get_nested(config, "run_log.output_file", "log.md"),
                force_rebuild=force_rebuild,
            )
        except Exception as e:
            _pipeline_logger.warning(f"Run log append failed: {e}")

    # Final cleanup of runtime-dependency artefacts the caller didn't keep
    # (index / assets / errors). Runs LAST — after every consumer (Vision,
    # knowledge_map, quality_report) has used them. No-op when the facade
    # didn't set output._cleanup (legacy full-output runs). Never raises.
    _finalize_artifacts(output_dir, config)

    return pipeline_result
