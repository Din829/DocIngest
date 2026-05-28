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


def _maybe_convert_xls(
    file_path: Path,
    config: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, dict[str, Any] | None]:
    """
    Legacy .xls (BIFF) → .xlsx via LibreOffice, so the rest of the pipeline
    can use the full xlsx path (openpyxl renderer + Vision + chunking).

    Returns (effective_path, transformation_record):
      - non-.xls or disabled → (file_path, None) untouched
      - LibreOffice missing  → (file_path, None) + warning, caller falls
                                through to TextParser
      - conversion failure   → (file_path, None) + warning, same fallback
      - success              → (cached_xlsx_path, {"step": "format_convert", ...})

    Cache: {output_dir}/.cache/_xls_convert/<sha256_of_original>.xlsx
    Keyed on the original .xls bytes so the same file is converted exactly
    once per output directory across runs.
    """
    if file_path.suffix.lower() != ".xls":
        return file_path, None
    if not get_nested(config, "parsing.xls.auto_convert_to_xlsx", True):
        return file_path, None

    import hashlib
    import shutil
    import subprocess
    import tempfile
    from .utils.binary_finder import find_binary

    soffice = find_binary("soffice", config)
    if not soffice:
        _pipeline_logger.warning(
            f".xls input {file_path.name}: LibreOffice not found, cannot "
            f"convert to .xlsx — will fall through to TextParser "
            f"(likely gibberish). Install LibreOffice or set "
            f"parsing.xls.auto_convert_to_xlsx: false to silence this."
        )
        return file_path, None

    try:
        # Stream-hash the source to avoid loading huge .xls into memory
        h = hashlib.sha256()
        with file_path.open("rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        digest = h.hexdigest()
    except OSError as e:
        _pipeline_logger.warning(
            f".xls input {file_path.name}: cannot read for hashing ({e}) — "
            f"skipping conversion"
        )
        return file_path, None

    cache_dir = output_dir / ".cache" / "_xls_convert"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_xlsx = cache_dir / f"{digest}.xlsx"

    if not cached_xlsx.exists():
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                proc = subprocess.run(
                    [soffice, "--headless", "--convert-to", "xlsx",
                     "--outdir", tmpdir, str(file_path)],
                    capture_output=True, timeout=180,
                )
                produced = list(Path(tmpdir).glob("*.xlsx"))
                if proc.returncode != 0 or not produced:
                    _pipeline_logger.warning(
                        f".xls → .xlsx conversion failed for "
                        f"{file_path.name} (exit={proc.returncode}): "
                        f"{proc.stderr.decode('utf-8', errors='replace')[:200]}"
                    )
                    return file_path, None
                shutil.move(str(produced[0]), cached_xlsx)
        except (subprocess.TimeoutExpired, OSError) as e:
            _pipeline_logger.warning(
                f".xls → .xlsx conversion error for {file_path.name}: {e}"
            )
            return file_path, None

    return cached_xlsx, {
        "step": "format_convert",
        "from": "xls",
        "to": "xlsx",
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
    import subprocess
    import tempfile
    from .parsers.base import PageData
    from .utils.binary_finder import find_binary

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
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf",
                 "--outdir", tmpdir, str(file_path)],
                capture_output=True, timeout=180,
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
      2. No structured data injection (no chart hook data needing visual context)
      3. Sufficient text extracted (not a scanned/image-only page)
      4. No garbled text (Docling didn't fail on this page)
      5. Low replacement character ratio (no CID font issues)
      6. No complex tables (simple tables are fine, complex ones need Vision)
      7. No mixed-script anomaly (CJK mismap garbling from OCR)
      8. Text scripts match the document's declared language (catches CMap
         failures that produce "clean" but wrong Unicode — e.g. Bengali /
         Thai / Tibetan characters in a document declared as Japanese).
    """
    text = page_data.text
    stripped = text.strip()

    # Has image/chart markers → Vision needs to describe visual elements
    if "<!-- image -->" in text:
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
    # &lt; / &gt; in running text = HTML entity artifacts from OCR garbling
    #   (e.g. 確&lt; from a garbled 確認). Legitimate HTML entities appear
    #   inside code blocks or tags, not in running Japanese/Chinese text.
    if "glyph<" in text or "glyph&lt;" in text:
        return False
    if "&lt;" in stripped or "&gt;" in stripped:
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

    # All checks passed → pure text page, safe to skip
    return True


# Pre-compiled pattern for mixed-script detection (module-level, compiled once)
_MIXED_SCRIPT_RE = re.compile(
    r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]"   # CJK / Hiragana / Katakana
    r"[A-Za-z]{1,3}"                                 # short ASCII fragment
    r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]"     # CJK / Hiragana / Katakana
)


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

    Optional triage (parsing.vision.triage.enabled) skips pages that are
    purely text with no visual elements — saving API cost without losing info.
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

    vision_tasks = []
    no_image = 0
    triage_skipped = 0
    cap_skipped = 0
    for i, page_data in enumerate(parse_result.pages):
        if not page_data.image_path:
            no_image += 1
            continue
        if triage_enabled and _should_skip_vision(
            page_data, structured_per_page, triage_cfg, doc_language
        ):
            triage_skipped += 1
            continue
        if max_vision_pages is not None and len(vision_tasks) >= max_vision_pages:
            cap_skipped += 1
            continue
        vision_tasks.append((i, page_data))

    if triage_skipped:
        logger.info(
            f"Vision triage: {triage_skipped} page(s) skipped (text-only), "
            f"{len(vision_tasks)} page(s) sent to Vision"
        )
    if cap_skipped:
        logger.warning(
            f"Vision cap: {cap_skipped} page(s) skipped — "
            f"max_pages={max_vision_pages} reached. "
            f"Increase parsing.vision.max_pages or set to null to remove limit."
        )

    if not vision_tasks:
        logger.info(
            f"Vision enrichment: 0 pages to process "
            f"(no_image={no_image}, triage_skipped={triage_skipped})"
        )
        return

    doc_format = parse_result.metadata.get("format")
    vision_timeout = get_nested(config, "models.vision.timeout_sec", 180)
    results: dict[int, str] = {}  # page_index → vision result text
    described = 0
    failed = 0

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

            # Scale timeout with batch size, capped so a hung API surfaces eventually.
            # Single-batch wall-clock cap is independent of how many batches run
            # in parallel — concurrency doesn't change per-call latency.
            batch_timeout = min(vision_timeout * len(batch_imgs), 600)

            try:
                batch_text = run_with_timeout(
                    lambda: describe_pages_batched_cached(
                        image_paths=batch_imgs,
                        page_texts=batch_texts,
                        config=config,
                        cache=cache,
                        structured_data=batch_struct,
                        doc_format=doc_format,
                    ),
                    batch_timeout,
                )
            except Exception as e:
                return batch_idx, None, f"{type(e).__name__}: {e}"
            if not (batch_text and batch_text.strip()):
                return batch_idx, None, "empty response"

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
                # Approx progress signal — order is "completion order" not
                # idx order, which matches what users expect for parallel work.
                logger.info(
                    f"  batch {idx + 1}/{n_batches}: described "
                    f"({len(wrapped):,} chars)"
                )

        if any_batch_failed:
            use_batched = False
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

        def _call_vision(idx: int, page_data) -> tuple[int, str | None]:
            from .utils.timeout import run_with_timeout
            try:
                # page_data.page_no is 1-based and matches the hook's
                # convention for populating structured_extractions_per_page.
                struct_data = structured_per_page.get(page_data.page_no)
                return idx, run_with_timeout(
                    lambda: describe_page_cached(
                        image_path=page_data.image_path,
                        page_text=page_data.text,
                        config=config,
                        cache=cache,
                        structured_data=struct_data,
                        doc_format=doc_format,
                    ),
                    vision_timeout,
                )
            except TimeoutError as e:
                logger.warning(f"Vision timed out for page {page_data.page_no}: {e}")
                return idx, None
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

    if cache_enabled:
        cache.close()

    mode_tag = "batched" if use_batched else f"parallel={get_nested(config, 'performance.parallel_files', 4)}"
    logger.info(
        f"Vision enrichment: {described} described, {failed} failed, "
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


# ---------------------------------------------------------------------------
# Docling + Vision deduplication
# ---------------------------------------------------------------------------

def _dedup_vision(markdown: str, config: dict[str, Any]) -> str:
    """
    Remove Docling-Vision content overlap in a markdown string.

    DEFAULT OFF — keep both Docling and Vision views (no content loss).
    The "Vision is a superset of Docling" assumption only holds reliably
    for single-page-per-section formats (text PDF, PPTX); for xlsx / docx
    a length-ratio check cannot tell whether the two views describe the
    same content, and the wrong side may silently overwrite the right side.

    When enabled (`output.dedup.enabled: true`) and a pagebreak section
    contains both a Docling block and a <!-- vision-enriched --> block:
      - Vision len >= threshold × Docling len AND Vision len >= min_chars
        → drop Docling, keep only Vision
      - Otherwise → keep BOTH (safe path)

    Sections WITHOUT vision-enriched stay untouched regardless of setting.
    Knobs: output.dedup.enabled / vision_ratio_threshold / vision_min_chars.
    """
    if not get_nested(config, "output.dedup.enabled", False):
        return markdown

    threshold = float(get_nested(
        config, "output.dedup.vision_ratio_threshold", 0.7
    ))
    min_chars = int(get_nested(
        config, "output.dedup.vision_min_chars", 200
    ))

    pagebreak = PAGEBREAK_MARKER
    sections = markdown.split(pagebreak)

    deduped_sections = []
    for section in sections:
        if "<!-- vision-enriched -->" in section:
            parts = section.split("<!-- vision-enriched -->", 1)
            pre_vision = parts[0].strip()
            vision_content = parts[1].strip()

            # Strip frontmatter from length calculation
            docling_text = pre_vision
            if pre_vision.startswith("---\n") and "\n---" in pre_vision[4:]:
                frontmatter_end = pre_vision.index("\n---", 4) + 4
                frontmatter = pre_vision[:frontmatter_end]
                docling_text = pre_vision[frontmatter_end:].strip()
            else:
                frontmatter = None

            # Safety check: only dedup if Vision captured enough content.
            # Two independent gates — both must pass to drop Docling:
            #   1. ratio: Vision is ≥ threshold × Docling length
            #   2. absolute floor: Vision is ≥ min_chars
            # Gate 2 catches the failure mode where a sparse page yields a
            # tiny Vision blob that proportionally matches a denoised Docling
            # block, silently wiping real content. Empty Docling sections
            # remain safe to replace (they have nothing to lose).
            docling_len = len(docling_text)
            vision_len = len(vision_content)
            vision_sufficient = (
                docling_len == 0
                or (
                    vision_len >= docling_len * threshold
                    and vision_len >= min_chars
                )
            )

            if vision_sufficient:
                # Vision is complete — keep only Vision version
                if frontmatter:
                    deduped_sections.append(frontmatter + "\n\n" + vision_content)
                else:
                    deduped_sections.append(vision_content)
            else:
                # Vision missed content — keep both to avoid info loss
                deduped_sections.append(section)
        else:
            deduped_sections.append(section)

    return pagebreak.join(deduped_sections)


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

    # --- Phase 0.5: Legacy format convert (.xls → .xlsx via LibreOffice) ---
    # When this fires, `file_path` is rebound to the cached .xlsx so every
    # downstream Phase (hooks / parser / page-image / metadata) flows
    # through the xlsx path unchanged. The ORIGINAL .xls path is held in
    # `result.original_file` (set above) and reused for lineage below, so
    # `metadata.lineage.original_input` still points at the user's input.
    xls_convert_record: dict[str, Any] | None = None
    file_path, xls_convert_record = _maybe_convert_xls(
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
    parse_timeout = get_nested(config, "parsing.timeout_sec", 300)
    try:
        from .utils.timeout import run_with_timeout
        parse_result = run_with_timeout(
            lambda: parser.parse(file_path, override_stream=override_stream),
            parse_timeout,
        )
    except TimeoutError as e:
        result.success = False
        result.error = f"Parse timed out: {e}"
        result.error_type = "timeout"
        return result, []
    except (FileNotFoundError, PermissionError, OSError) as e:
        result.success = False
        result.error = f"Parse failed (io): {e}"
        result.error_type = "io_error"
        return result, []
    except Exception as e:
        result.success = False
        result.error = f"Parse failed: {e}"
        result.error_type = "parse_error"
        return result, []
    result.parse_time_ms = int((time.monotonic() - t0) * 1000)

    if not parse_result.success:
        # Check error handling config
        on_failure = get_nested(config, "error_handling.on_parse_failure", "skip")
        if on_failure == "fail":
            result.success = False
            result.error = parse_result.error
            result.error_type = "parse_error"
            return result, []
        # "skip" — mark as failed but continue pipeline for other files
        result.success = False
        result.error = parse_result.error
        result.error_type = "parse_error"
        return result, []

    result.format = parse_result.metadata.get("format", "unknown")

    # Lineage record — provenance order is the order things ran:
    #   format_convert (Phase 0.5) → pre_parse hook (Phase 1.0) → parser.
    # xls_convert_record is prepended so the lineage trail starts at the
    # user's original .xls input, then shows the conversion, then the
    # rest of the pipeline operating on the converted .xlsx.
    if xls_convert_record is not None:
        parse_result.transformations.insert(0, xls_convert_record)
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

    # Phase 0.5 aftermath: when a .xls was auto-converted upstream, the parser
    # saw the cached file (stem = sha256). Restore the user's original stem
    # so derived metadata (frontmatter title, derive_aliases_hook output)
    # reflects the input file. Done BEFORE Phase 1.6 pre_write hooks so
    # derive_aliases_hook reads the corrected title and produces clean aliases.
    if xls_convert_record is not None:
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
    if result.format in ("xlsx", "xls"):
        _ensure_excel_page_images(file_path, parse_result, config)
    elif result.format in ("docx", "doc"):
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
        _enrich_with_vision(parse_result, config)

    # --- Phase 1.6: Pre-write hooks (post-Vision, pre-frontmatter) ---
    # Hooks that enrich metadata without touching Vision (e.g. exiftool
    # metadata extraction). Runs after Vision so language detection and
    # Vision results are already settled before frontmatter is built.
    run_post_parse_hooks(file_path, parse_result, config, phase="pre_write")

    # --- Phase 1.7: Docling + Vision dedup ---
    # When Vision enrichment produced a version of each page, deduplicate
    # so sources/*.md and chunks.jsonl both get clean, non-redundant content.
    # Controlled by output.dedup.enabled (default: true).
    parse_result.markdown = _dedup_vision(parse_result.markdown, config)

    # --- Phase 2: Write Markdown + assets ---
    # original_file determines the sources/*.md filename — must be the
    # user's input, not the Phase-0.5-converted xlsx in .cache/.
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
            # File-level identity / Docling origin — promoted to top-level
            # frontmatter keys by the file_metadata hook; chunk gets the
            # same info via `lineage.original_input.{filename,mimetype,
            # binary_hash}`.
            "docling_origin",
            "docling_name",
            "mimetype",
            "binary_hash",
            # File-level embedded-asset registry. The full list lives in
            # index.json (per-file "files[].assets"); chunk-side image
            # references are conveyed via inline `<!-- image: ... -->`
            # markers in the chunk text itself.
            "xlsx_embedded_images",
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

    # Collect non-fatal warnings produced during this file's processing
    # (page caps, OCR fallbacks, etc.) for run_pipeline to aggregate.
    # parse_result.metadata["warnings"] is a list[str] written by phases
    # like _generate_page_images_via_libreoffice.
    md_warnings = parse_result.metadata.get("warnings")
    if isinstance(md_warnings, list):
        result.warnings = [str(w) for w in md_warnings if w]

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

    Returns:
        PipelineResult with details of all processed files. When safety
        strict mode aborts the run, result.safety.aborted is True and
        result.total_files reflects the discovered count while
        result.successful and result.files stay empty.
    """
    t_start = time.monotonic()

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
    safety_mode = str(get_nested(config, "safety.mode", "warn") or "warn").lower()
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

    try:
        for _idx, (file_path, prior_meta, cache_reason) in enumerate(to_process):
            if _stop_requested["flag"]:
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
            )

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

    # Write errors.json if any failures
    report_file = get_nested(config, "error_handling.report_file", "errors.json")
    if pipeline_result.errors:
        errors_path = output_dir / report_file
        errors_path.write_text(
            json.dumps(pipeline_result.errors, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

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

    return pipeline_result
