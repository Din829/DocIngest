"""
Document inspection — lightweight pre-flight check before pipeline processing.

Quickly scans input files to report size, page count, format, and estimated
processing characteristics WITHOUT actually parsing them. Designed to be
called by Agents, MCP tools, or humans before ``docingest run``.

Usage:
    CLI:     docingest inspect report.pdf slides.pptx ./docs/
    Python:  from docingest.inspect import inspect_files
             results = inspect_files([Path("report.pdf")])

Performance: sub-second for most files (reads only metadata, not content).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import get_nested

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-format inspectors (fast metadata only, no full parsing)
# ---------------------------------------------------------------------------

def _inspect_pdf(file_path: Path) -> dict[str, Any]:
    """Inspect PDF: page count via PyMuPDF (reads xref table only, very fast)."""
    try:
        import pymupdf
        doc = pymupdf.open(str(file_path))
        page_count = len(doc)
        doc.close()
        return {"pages": page_count}
    except Exception as e:
        logger.debug(f"PDF inspection failed for {file_path.name}: {e}")
        return {"pages": None, "error": str(e)}


def _inspect_pptx(file_path: Path) -> dict[str, Any]:
    """Inspect PPTX: slide count via python-pptx."""
    try:
        from pptx import Presentation
        prs = Presentation(str(file_path))
        return {"pages": len(prs.slides)}
    except Exception as e:
        logger.debug(f"PPTX inspection failed for {file_path.name}: {e}")
        return {"pages": None, "error": str(e)}


def _inspect_xlsx(file_path: Path) -> dict[str, Any]:
    """Inspect XLSX: sheet count + total rows estimate via openpyxl read-only."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(str(file_path), read_only=True, data_only=True)
        sheets = wb.sheetnames
        total_rows = 0
        for name in sheets:
            ws = wb[name]
            total_rows += ws.max_row or 0
        wb.close()
        return {"pages": len(sheets), "sheets": sheets, "total_rows": total_rows}
    except Exception as e:
        logger.debug(f"XLSX inspection failed for {file_path.name}: {e}")
        return {"pages": None, "error": str(e)}


def _inspect_docx(file_path: Path) -> dict[str, Any]:
    """Inspect DOCX: approximate page count from word count heuristic."""
    try:
        from docx import Document
        doc = Document(str(file_path))
        word_count = sum(len(p.text.split()) for p in doc.paragraphs)
        # Rough heuristic: ~300 words per page
        est_pages = max(1, word_count // 300)
        return {"pages": est_pages, "words": word_count, "pages_estimated": True}
    except Exception as e:
        logger.debug(f"DOCX inspection failed for {file_path.name}: {e}")
        return {"pages": None, "error": str(e)}


def _inspect_zip(file_path: Path) -> dict[str, Any]:
    """Inspect ZIP: file count and total uncompressed size."""
    try:
        import zipfile
        with zipfile.ZipFile(str(file_path), "r") as zf:
            entries = zf.infolist()
            file_count = sum(1 for e in entries if not e.is_dir())
            total_size = sum(e.file_size for e in entries)
            return {
                "pages": file_count,
                "files_inside": file_count,
                "uncompressed_size_mb": round(total_size / (1024 * 1024), 1),
            }
    except Exception as e:
        logger.debug(f"ZIP inspection failed for {file_path.name}: {e}")
        return {"pages": None, "error": str(e)}


# Format → inspector mapping
_INSPECTORS: dict[str, Any] = {
    "pdf": _inspect_pdf,
    "pptx": _inspect_pptx,
    "ppt": _inspect_pptx,
    "xlsx": _inspect_xlsx,
    "xls": _inspect_xlsx,
    "docx": _inspect_docx,
    "doc": _inspect_docx,
    "zip": _inspect_zip,
}


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------

def _recommend(info: dict[str, Any], config: dict[str, Any]) -> str:
    """Generate a human/agent-readable recommendation based on inspection."""
    pages = info.get("pages")
    size_mb = info.get("size_mb", 0)
    max_vision = get_nested(config, "parsing.vision.max_pages", 50)

    warnings: list[str] = []

    if pages is not None and max_vision is not None and pages > int(max_vision):
        warnings.append(f"Vision capped at {max_vision} pages (has {pages})")

    if size_mb > 100:
        warnings.append(f"Large file ({size_mb:.0f}MB) — parsing may be slow")
    elif size_mb > 50:
        warnings.append(f"Medium-large file ({size_mb:.0f}MB)")

    if info.get("total_rows", 0) > 50000:
        warnings.append(f"Large spreadsheet ({info['total_rows']:,} rows)")

    if info.get("error"):
        warnings.append(f"Inspection error: {info['error']}")

    if not warnings:
        return "Ready"
    return "; ".join(warnings)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inspect_single(file_path: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Inspect a single file without parsing it.

    Returns a dict with: name, format, size_mb, pages, recommendation, and
    any format-specific fields (sheets, words, files_inside, etc.).
    """
    if config is None:
        config = {}

    suffix = file_path.suffix.lstrip(".").lower()
    size_mb = file_path.stat().st_size / (1024 * 1024)

    info: dict[str, Any] = {
        "name": file_path.name,
        "path": str(file_path),
        "format": suffix,
        "size_mb": round(size_mb, 2),
    }

    # Run format-specific inspector
    inspector = _INSPECTORS.get(suffix)
    if inspector:
        info.update(inspector(file_path))
    else:
        # Unknown format — just report size
        info["pages"] = None

    info["recommendation"] = _recommend(info, config)
    return info


def inspect_files(
    input_paths: list[Path | str],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Inspect multiple files/directories.

    Expands directories recursively (same logic as pipeline.discover_files).
    Returns list of inspection results, one per file.
    """
    from .pipeline import discover_files

    if config is None:
        from .config import load_config
        config = load_config()

    files = discover_files([Path(p) if isinstance(p, str) else p for p in input_paths], config)
    return [inspect_single(f, config) for f in files]
