"""
PDF embedded-attachment expansion for DocIngest input discovery.

Problem
-------
A PDF can carry whole files in its ``/EmbeddedFiles`` name tree — like email
attachments, not page images. Acrobat shows them in the "Attachments" panel;
the page content never references them. Docling (and therefore DocIngest)
parses the page body and silently ignores these attachments, so their content
never reaches the knowledge base — and nothing is reported as missing.

This is the PDF analogue of ZIP expansion: a container file holds independent
files that must be flattened into the discovery list so each gets parsed by the
right path (image → Vision, child PDF → PDF parser, xlsx → openpyxl renderer).

Design
------
Mirrors ``zip_expander.py`` deliberately — same extract root, same flattened
``<parent_stem>__<inner_name>`` naming (reused via ``_flatten_inner_path``),
same graceful-degradation contract. The ONE structural difference:

* A ZIP *is* the content — after expansion the archive is dropped from the
  list. A PDF with attachments still has its OWN page body worth parsing, so
  the parent PDF stays in the list and attachments are *appended*. Extraction
  here never removes the parent.

Key decisions
~~~~~~~~~~~~~
* **One file open, not two** — ``expand_pdf_attachments`` opens the PDF once and
  both counts and extracts. There is no separate "probe" pass that re-opens the
  file; the caller just calls expand and gets back ``[]`` when there's nothing.

* **Persistent extract root** — same ``{output.dir}/.cache/_pdf_attachments/``
  lifecycle as zip, so a second run reuses extracted files and the
  content-addressed cache still kicks in at the pipeline level.

* **Graceful degradation** — a per-attachment failure is logged and skipped
  (the others still extract); a whole-file failure (corrupt PDF, no pymupdf)
  logs a warning and returns ``[]`` so the parent PDF is processed normally.
  NEVER raises — same contract as zip_expander and the hooks layer.

Non-goals
~~~~~~~~~
* We do not recurse into attachments-of-attachments. A child PDF that itself
  carries attachments is parsed as a normal PDF; its own attachments are not
  re-expanded (the discovery pass runs once over the input list). If that
  becomes a real need, hoist this into the same fixpoint loop ZIP uses.
* We do not decrypt password-protected attachments.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .zip_expander import _flatten_inner_path

logger = logging.getLogger(__name__)


def expand_pdf_attachments(
    pdf_path: Path,
    extract_root: Path,
) -> list[Path]:
    """
    Extract every ``/EmbeddedFiles`` attachment from a PDF into a deterministic
    subdirectory of ``extract_root`` and return the produced file paths.

    Returns ``[]`` when the file is not a PDF, carries no attachments, can't be
    opened, or pymupdf is unavailable — in every case the caller keeps the
    parent PDF and processes it normally. Never raises.

    Naming mirrors zip expansion: ``<pdf_stem>__<attachment_name>`` via
    ``_flatten_inner_path``, so provenance is traceable and names don't collide
    across parents.

    Args:
        pdf_path: absolute path to the PDF file.
        extract_root: base directory under which attachments are extracted.
            Each parent PDF gets its own subdirectory named after its stem.
    """
    if pdf_path.suffix.lower() != ".pdf":
        return []

    try:
        import pymupdf
    except ImportError:
        logger.debug("pymupdf unavailable; skipping PDF attachment extraction")
        return []

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:  # noqa: BLE001 — never raise; parent still parses
        logger.warning(
            f"Cannot open {pdf_path.name} for attachment extraction: {e}"
        )
        return []

    produced: list[Path] = []
    try:
        count = doc.embfile_count()
        if count == 0:
            return []

        target_dir = extract_root / pdf_path.stem
        target_dir.mkdir(parents=True, exist_ok=True)

        for k in range(count):
            try:
                info = doc.embfile_info(k)
                inner_name = (
                    info.get("filename") or info.get("name") or f"attachment_{k}"
                )
                data = doc.embfile_get(k)
            except Exception as e:  # noqa: BLE001 — isolate a single bad entry
                logger.warning(
                    f"Skipping attachment #{k} in {pdf_path.name}: {e}"
                )
                continue

            out_name = _flatten_inner_path(pdf_path.stem, inner_name)
            out_path = target_dir / out_name

            # Guard against an attachment name escaping the extract root.
            try:
                out_path.resolve().relative_to(extract_root.resolve())
            except ValueError:
                logger.warning(
                    f"Attachment path escape blocked: {inner_name!r} "
                    f"in {pdf_path.name}"
                )
                continue

            out_path.write_bytes(data)
            produced.append(out_path)
    except Exception as e:  # noqa: BLE001 — whole-file failure → parent only
        logger.warning(
            f"PDF attachment extraction failed for {pdf_path.name}: {e}"
        )
        return produced
    finally:
        doc.close()

    if produced:
        logger.info(
            f"PDF attachments: {pdf_path.name} → {len(produced)} file(s) "
            f"→ {extract_root / pdf_path.stem}"
        )
    return produced
