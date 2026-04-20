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
    # Quality report summary (populated after Phase 4 if enabled).
    # Keys: total_files, files_with_issues, total_questions, total_unreadable,
    #       quality_score, files (only files with issues).
    quality: dict[str, Any] = field(default_factory=dict)
    # LLM API token usage summary (populated at pipeline end).
    # Keys: total_prompt_tokens, total_completion_tokens, total_tokens,
    #       total_calls, total_cache_hits, by_model.
    token_usage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_files(
    input_paths: list[Path | str],
    config: dict[str, Any] | None = None,
) -> list[Path]:
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
        Flat list of individual file paths (directories + archives + URLs
        recursively expanded).
    """
    # First pass: separate URLs from filesystem paths. We work with raw
    # strings first because Path("https://...") on Windows mangles the
    # URL (double slash → single slash), making URL detection unreliable
    # if done after Path conversion.
    raw_files: list[Path] = []
    url_inputs: list[str] = []

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

    # Resolve URL inputs → local files (audio + subtitles + metadata).
    # Only when config is available and URL parsing is enabled.
    if url_inputs and config is not None and get_nested(config, "parsing.url.enabled", True):
        from .utils.url_resolver import resolve_url
        for url in url_inputs:
            resolved = resolve_url(url, config)
            if resolved:
                raw_files.extend(resolved)
                _pipeline_logger.info(
                    f"URL resolved: {url} → {len(resolved)} file(s)"
                )
            else:
                _pipeline_logger.warning(f"URL resolution failed: {url}")

    # Second pass: expand zip archives when enabled. We run this after
    # the directory walk so zip files found INSIDE an input directory
    # are also expanded, not just zips passed directly on the command
    # line.
    if config is None or not get_nested(config, "parsing.zip.enabled", True):
        return raw_files

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

    return expanded


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
    """
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


def _generate_page_images_via_libreoffice(
    file_path: Path,
    parse_result,
    config: dict[str, Any],
    max_pages: int,
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
            # Step 1: → PDF via LibreOffice
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
                    if i >= max_pages:
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
            except ImportError:
                # pdf2image unavailable → try Docling PDF re-parse
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
                        if len(pages_data) >= max_pages:
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
                except Exception:
                    pass

            # Apply results
            if pages_data:
                parse_result.pages = pages_data
                _pipeline_logger.info(
                    f"{format_label} page images: generated {len(pages_data)} pages "
                    f"via LibreOffice for {file_path.name}"
                )

            # Emit warning if pages were capped (visible in frontmatter + logs)
            if total_pages > max_pages:
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
        _pipeline_logger.debug(f"{format_label} page image generation failed: {e}")


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

    # Parallel Vision calls
    parallel = get_nested(config, "performance.parallel_files", 4)
    results: dict[int, str] = {}  # page_index → vision result text
    described = 0
    failed = 0

    doc_format = parse_result.metadata.get("format")

    def _call_vision(idx: int, page_data) -> tuple[int, str | None]:
        try:
            # page_data.page_no is 1-based and matches the hook's
            # convention for populating structured_extractions_per_page.
            struct_data = structured_per_page.get(page_data.page_no)
            return idx, describe_page_cached(
                image_path=page_data.image_path,
                page_text=page_data.text,
                config=config,
                cache=cache,
                structured_data=struct_data,
                doc_format=doc_format,
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
        # Mode A: align by section index
        for idx, text in results.items():
            if idx < len(sections):
                sections[idx] = (
                    sections[idx].rstrip()
                    + f"\n\n<!-- vision-enriched -->\n{text}\n"
                )
        parse_result.markdown = pagebreak.join(sections)
    else:
        # Mode B: append all Vision results in page order at end of document
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

    logger.info(
        f"Vision enrichment: {described} described, {failed} failed, "
        f"{no_image} no-image, {triage_skipped} triage-skipped, "
        f"{cap_skipped} cap-skipped (parallel={parallel})"
    )


# ---------------------------------------------------------------------------
# Docling + Vision deduplication
# ---------------------------------------------------------------------------

def _dedup_vision(markdown: str, config: dict[str, Any]) -> str:
    """
    Remove Docling-Vision content overlap in a markdown string.

    Applied to BOTH sources/*.md and chunking input (unified logic).
    Vision receives Docling text as prompt context, so its output is
    typically a superset of Docling's content. Keeping both creates
    redundancy that hurts grep (Agentic Search) and inflates tokens.

    For each pagebreak section with both Docling text and
    <!-- vision-enriched --> content:
      - Vision content >= threshold × Docling length → keep only Vision
      - Vision content < threshold → keep BOTH (Vision may have missed content)

    Sections WITHOUT vision-enriched stay untouched.
    Controlled by output.dedup.enabled and output.dedup.vision_ratio_threshold.
    """
    if not get_nested(config, "output.dedup.enabled", True):
        return markdown

    threshold = float(get_nested(
        config, "output.dedup.vision_ratio_threshold", 0.7
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

            # Safety check: only dedup if Vision captured enough content
            docling_len = len(docling_text)
            vision_len = len(vision_content)
            vision_sufficient = (
                docling_len == 0
                or vision_len >= docling_len * threshold
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

    # --- Phase 1.0: Pre-parse hooks (e.g. DOCX OMML → LaTeX preprocessing) ---
    # Hooks can return a BytesIO stream that replaces the file content before
    # Docling sees it. None means "use original file". Hooks never raise —
    # failures degrade to the original file with a warning.
    override_stream = run_pre_parse_hooks(file_path, config)

    # --- Phase 1: Parse ---
    t0 = time.monotonic()
    try:
        parse_result = parser.parse(file_path, override_stream=override_stream)
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

        result.chunks_count = len(chunks)
        result.chunk_time_ms = int((time.monotonic() - t1) * 1000)

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

    return ParseResult(
        markdown=markdown,
        metadata=metadata,
    )


def run_pipeline(
    input_paths: list[Path | str],
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

    # Discover files (with zip expansion when enabled)
    files = discover_files(input_paths, config=config)
    pipeline_result = PipelineResult(total_files=len(files))

    if not files:
        pipeline_result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return pipeline_result

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

    # Partition files: cached vs. to_process
    cached_files: list[tuple[Path, dict[str, Any], str]] = []  # (path, meta, cache_key)
    to_process: list[Path] = []

    if incremental_enabled and not force_rebuild:
        for file_path in files:
            try:
                cache_key = compute_cache_key(file_path)
            except (FileNotFoundError, PermissionError, OSError) as e:
                _pipeline_logger.warning(f"Cannot hash {file_path.name}: {e}")
                to_process.append(file_path)
                continue

            meta = load_cached_meta(cache_dir, cache_key)
            if meta is None:
                to_process.append(file_path)
                continue

            valid, reason = is_cache_valid(meta, config_hash, output_dir, old_chunks_by_id)
            if valid:
                cached_files.append((file_path, meta, cache_key))
            else:
                _pipeline_logger.debug(
                    f"Cache miss for {file_path.name}: {reason}"
                )
                to_process.append(file_path)

        _pipeline_logger.info(
            f"Incremental: {len(cached_files)} cached, {len(to_process)} to process "
            f"(out of {len(files)} total)"
        )
    else:
        to_process = list(files)
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

    # --- Process uncached files: full pipeline ---
    for file_path in to_process:
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
            })
            index_builder.add_error()

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

    # Collect LLM API token usage
    from .models.token_tracker import token_tracker
    pipeline_result.token_usage = token_tracker.summary()
    token_tracker.reset()

    return pipeline_result
