"""
DOCX OMML → LaTeX pre-parse hook (#2).

Why this hook exists
--------------------
Microsoft Word stores mathematical equations in OMML (Office Math Markup
Language), an XML format embedded inside word/document.xml. Docling's
DOCX parser does NOT convert OMML to LaTeX — formulas are typically lost
or reduced to glyph placeholders in the exported Markdown. For
technical specs, academic papers, and engineering documents this is a
hard data loss.

What we do
----------
Before Docling sees the .docx file, we:
  1. Open the .docx as a zip (it's a ZIP archive of XML files).
  2. For the three XML parts that can contain math:
       word/document.xml
       word/footnotes.xml
       word/endnotes.xml
     walk the XML tree and replace every <m:oMath>/<m:oMathPara> element
     with a <w:r><w:t>$...$</w:t></w:r> run holding the LaTeX
     equivalent. Inline math uses single-dollar wrapping, display math
     (oMathPara) uses double-dollar.
  3. Re-zip the modified files into a new DOCX and return it as a
     BytesIO. The pipeline then feeds this stream to Docling via the
     override_stream parameter — Docling sees what looks like a normal
     .docx but with LaTeX text in place of formulas.

Non-goals
---------
  * We do NOT touch headers/footers math (low-value + adds complexity).
  * We do NOT modify the original file on disk — the rewritten DOCX
    lives only in memory.
  * We do NOT raise — any failure returns None and lets the pipeline
    parse the original file.

Implementation note
-------------------
The heavy lifting (OMML → LaTeX) lives in _docx_math/, ported verbatim
from Microsoft MarkItDown (MIT, adapted from xiilei/dwml). See
_docx_math/__init__.py for provenance.

Config:
  parsing.docx.omml_to_latex — master toggle (default true)
"""

from __future__ import annotations

import logging
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from ..config import get_nested

logger = logging.getLogger(__name__)


# XML parts inside a DOCX that can contain <m:oMath> / <m:oMathPara>.
# Headers/footers are intentionally excluded — low value, extra complexity.
_MATH_XML_PARTS = (
    "word/document.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
)


def _convert_omath_to_latex(tag: Any) -> str:
    """
    Convert a single BeautifulSoup <oMath> tag to a LaTeX string.

    Uses _docx_math.omml (ported from MarkItDown/dwml).
    """
    # Lazy import so _docx_math is only loaded when the hook actually runs
    from xml.etree import ElementTree as ET

    from ._docx_math.omml import OMML_NS, oMath2Latex

    # oMath2Latex expects a fully-formed XML element inside a document
    # with the correct math namespace declared on the root. Wrap the
    # oMath fragment in a minimal document with the right xmlns so
    # ElementTree can find the namespaced oMath element.
    math_root_template = (
        '<w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        "{0}</w:document>"
    )
    math_root = ET.fromstring(math_root_template.format(str(tag)))
    math_element = math_root.find(OMML_NS + "oMath")
    if math_element is None:
        # Tag wasn't actually an oMath element — bail
        raise ValueError("oMath element not found in wrapped fragment")
    return oMath2Latex(math_element).latex


def _build_replacement_run(tag: Any, *, block: bool) -> Any:
    """
    Create a <w:r><w:t>$latex$</w:t></w:r> replacement tag.

    block=True wraps in $$...$$ (display math), block=False uses $...$
    (inline math).
    """
    from bs4 import Tag

    latex = _convert_omath_to_latex(tag)
    wrapped = f"$${latex}$$" if block else f"${latex}$"

    t_tag = Tag(name="w:t")
    t_tag.string = wrapped
    r_tag = Tag(name="w:r")
    r_tag.append(t_tag)
    return r_tag


def _replace_equations_in_xml(xml_bytes: bytes) -> bytes:
    """
    Walk a DOCX XML part and replace all math elements with LaTeX text runs.

    Returns the modified XML bytes. If nothing was replaced, returns the
    original bytes unchanged (cheap no-op for documents without formulas).
    """
    from bs4 import BeautifulSoup

    text = xml_bytes.decode("utf-8", errors="replace")

    # Quick short-circuit: no math tags → don't even parse XML
    if "<m:oMath" not in text:
        return xml_bytes

    soup = BeautifulSoup(text, features="xml")
    replaced = 0

    # oMathPara = block (display) equations. Each oMathPara contains one
    # or more oMath children; we replace the whole paragraph with a new
    # <w:p> that carries the LaTeX text runs in block form.
    for para in soup.find_all("oMathPara"):
        try:
            from bs4 import Tag
            p_tag = Tag(name="w:p")
            for child in para.find_all("oMath"):
                try:
                    p_tag.append(_build_replacement_run(child, block=True))
                    replaced += 1
                except Exception as e:
                    logger.debug(f"OMML block conversion failed: {e}")
            para.replace_with(p_tag)
        except Exception as e:
            logger.debug(f"oMathPara replacement failed: {e}")
            continue

    # oMath = inline equations (any remaining not already inside oMathPara).
    for tag in soup.find_all("oMath"):
        try:
            tag.replace_with(_build_replacement_run(tag, block=False))
            replaced += 1
        except Exception as e:
            logger.debug(f"OMML inline conversion failed: {e}")
            continue

    if replaced == 0:
        return xml_bytes

    return str(soup).encode("utf-8")


def _rewrite_docx(original_bytes: bytes) -> tuple[BytesIO, int] | None:
    """
    Open a DOCX (as bytes), rewrite math XML parts, return new BytesIO
    plus the number of replaced equations.

    Returns None if the input is not a valid zip / any critical step
    fails. Documents with zero formulas return a stream with replaced=0
    (caller can decide to skip).
    """
    try:
        src_buffer = BytesIO(original_bytes)
        dst_buffer = BytesIO()
        total_replaced = 0

        with zipfile.ZipFile(src_buffer, mode="r") as src_zip:
            with zipfile.ZipFile(
                dst_buffer, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as dst_zip:
                for item in src_zip.infolist():
                    data = src_zip.read(item.filename)
                    if item.filename in _MATH_XML_PARTS:
                        try:
                            new_data = _replace_equations_in_xml(data)
                            # Count replacements by counting the delta of
                            # dollar-sign markers introduced (cheap heuristic
                            # — we already count in _replace_equations_in_xml
                            # but don't return it; instead we detect if data
                            # changed at all to confirm something happened).
                            if new_data != data:
                                total_replaced += 1  # at least one part changed
                            data = new_data
                        except Exception as e:
                            logger.debug(
                                f"OMML rewrite failed for {item.filename}: {e}"
                            )
                    dst_zip.writestr(item, data)

        dst_buffer.seek(0)
        return dst_buffer, total_replaced

    except zipfile.BadZipFile:
        logger.debug("DOCX is not a valid zip file")
        return None
    except Exception as e:
        logger.debug(f"DOCX rewrite failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def docx_omml_preprocess_hook(
    file_path: Path,
    config: dict[str, Any],
) -> BytesIO | None:
    """
    Pre-parse hook: rewrite DOCX math OMML → LaTeX before Docling parses.

    Returns a BytesIO stream of the rewritten DOCX when math was found
    and successfully converted, or None to leave the original file
    untouched.

    Never raises — any failure logs a debug message and returns None.
    """
    if not get_nested(config, "parsing.docx.omml_to_latex", True):
        return None

    if file_path.suffix.lower() != ".docx":
        return None

    # Cheap first check: if we can't even open the file, bail fast.
    try:
        original_bytes = file_path.read_bytes()
    except Exception as e:
        logger.debug(f"Cannot read {file_path.name} for OMML preprocessing: {e}")
        return None

    # Cheaper pre-check: skip files that have no math content at all
    # without incurring the BeautifulSoup parse cost. DOCX math uses the
    # <m:oMath> tag namespace — a literal byte scan of the zip entries
    # isn't reliable because entries are compressed, so we only skip
    # based on the final output being unchanged after trying.
    result = _rewrite_docx(original_bytes)
    if result is None:
        return None

    stream, replaced = result
    if replaced == 0:
        # No math in this document — return None so the pipeline
        # uses the original file path (faster than reading from stream).
        return None

    logger.info(
        f"DOCX OMML preprocessing: rewrote {replaced} math XML part(s) in {file_path.name}"
    )
    return stream
